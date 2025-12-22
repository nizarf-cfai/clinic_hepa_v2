import asyncio
import websockets
import json
import base64
import audioop
import os
import sys
import queue
import threading
import pyaudio
import string
import secrets
from dotenv import load_dotenv
from google.cloud import speech
from google.generativeai import types

# Local Imports
import diagnosis_manager
import question_manager
import agents
import gcs_manager

load_dotenv()

# --- CONFIG ---
WS_URL = "wss://clinic-hepa-backend-481780815788.us-central1.run.app/ws/simulation"
SIMULATION_RATE = 24000
TRANSCRIBER_RATE = 16000
OUTPUT_FILE = "simulation_transcript.txt"

def generate_session_id(length=5):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

class STTBridge:
    def __init__(self, patient_id: str, loop):
        self.loop = loop
        self.patient_id = patient_id
        self.session_id = generate_session_id()
        self.running = True
        self.resample_state = None
        
        # Memory & Fast I/O
        self.full_transcript_memory = ""
        self.file_handle = open(OUTPUT_FILE, "w", encoding="utf-8", buffering=1)
        
        # Agents
        self.hepa_agent = agents.DiagnosisHepato()
        self.gen_agent = agents.DiagnosisGeneral()
        self.consolidate_agent = agents.DiagnosisConsolidate()
        self.merger_agent = agents.QuestionMerger()
        self.supervisor = agents.InterviewSupervisor()

        self.audio_queue = queue.Queue()       
        self.transcript_queue = asyncio.Queue() 

        self.p = pyaudio.PyAudio()
        self.output_stream = self.p.open(
            format=pyaudio.paInt16, channels=1, rate=SIMULATION_RATE, output=True
        )

        self.dm = diagnosis_manager.DiagnosisManager()
        self.qm = question_manager.QuestionPoolManager([])
        self.patient_info = gcs_manager.GCSManager().read_text(f"patient_profile/{self.patient_id}/patient_info.md")

    def write_to_file_fast(self, text):
        self.file_handle.write(f"{text}\n")
        self.file_handle.flush()
        os.fsync(self.file_handle.fileno()) 
        self.full_transcript_memory += f"{text}\n"

    async def side_process_worker(self):
        """AI Processing Task. Can be cancelled immediately."""
        print(f"⚙️ Side Processor Active [ID: {self.session_id}]")
        count = 0
        try:
            while self.running:
                text = await self.transcript_queue.get()
                if text is None: break
                
                print(f"✅ AI Cycle {count} Start")
                # Snapshot context
                current_transcript = self.full_transcript_memory
                
                # Perform AI work (wrapped in gather for speed)
                h_task = self.hepa_agent.get_hepa_diagnosis(current_transcript, self.patient_info)
                g_task = self.gen_agent.get_gen_diagnosis(current_transcript, self.patient_info)

                
                # Using shield or wait_for if you wanted to limit time, 
                # but standard await is fine as we cancel the whole task on exit.
                hepa_diag, gen_diag = await asyncio.gather(h_task, g_task)
                print(f"✅ Hepa & General {count} Finished")


                consolidated = await self.consolidate_agent.consolidate_diagnosis(
                    self.dm.diagnoses, hepa_diag + gen_diag
                )
                print(f"✅ Consolidated {count} Finished")

                with open(f'output/consolidated_{count}.json', 'w', encoding='utf-8') as f:
                    json.dump(consolidated, f, indent=4)

                self.dm.diagnoses = consolidated

                status = await self.supervisor.check_completion(current_transcript, consolidated)
                ranked_questions = await self.merger_agent.process_question(
                    current_transcript, consolidated, self.qm.get_questions_basic()
                )
                self.qm.add_questions(ranked_questions)
                with open(f'output/get_questions_{count}.json', 'w', encoding='utf-8') as f:
                    json.dump(self.qm.get_questions(), f, indent=4)

                with open(f'status_update.json', 'w') as f:
                    json.dump({
                        "session_id": self.session_id, 
                        "is_finished": status.get("end", False),
                        "question":self.qm.get_high_rank_question().get("content")
                        }, f)
                
                print(f"✅ AI Cycle {count} Complete")
                count += 1
                self.transcript_queue.task_done()
        except asyncio.CancelledError:
            print("⚙️ Side Processor: Cancelled mid-operation.")
        finally:
            print("⚙️ Side Processor: Shut down.")

    def audio_generator(self):
        """Yields audio to Google STT. Stops immediately on None."""
        while True:
            chunk = self.audio_queue.get()
            if chunk is None or not self.running:
                return # This kills the Google Stream
            yield chunk

    def start_transcription_loop(self):
        client = speech.SpeechClient()
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=TRANSCRIBER_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_long",
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=True)

        while self.running:
            requests = (speech.StreamingRecognizeRequest(audio_content=c) for c in self.audio_generator())
            try:
                responses = client.streaming_recognize(streaming_config, requests)
                for response in responses:
                    if not self.running: break
                    if not response.results: continue
                    result = response.results[0]
                    if result.is_final:
                        transcript = result.alternatives[0].transcript
                        self.write_to_file_fast(transcript)
                        self.loop.call_soon_threadsafe(self.transcript_queue.put_nowait, transcript)
            except Exception as e:
                if self.running: print(f"STT Stream Error: {e}")

    async def connect_to_server(self, side_task):
        """Main WebSocket loop."""
        print(f"🔌 Connecting to Server...")
        try:
            async with websockets.connect(WS_URL) as ws:
                await ws.send(json.dumps({"type": "start", "patient_id": self.patient_id, "gender":"Male"}))
                
                while self.running:
                    msg = await ws.recv()
                    data = json.loads(msg) if isinstance(msg, str) else None
                    
                    # --- CRITICAL: IMMEDIATE SHUTDOWN TRIGGER ---
                    if data and data.get("type") == "turn" and data.get("data") == "end":
                        print("\n🏁 END signal received. Terminating now.")
                        self.running = False
                        side_task.cancel() # Stop AI agents mid-call
                        self.audio_queue.put(None) # Kill STT thread
                        break

                    if data and data.get("type") == "audio":
                        await self.process_audio(base64.b64decode(data["data"]))
                    elif isinstance(msg, bytes):
                        await self.process_audio(msg)
        except Exception as e:
            print(f"WS Error: {e}")

    async def process_audio(self, audio_bytes):
        if self.output_stream:
            try: self.output_stream.write(audio_bytes)
            except: pass
        converted, self.resample_state = audioop.ratecv(
            audio_bytes, 2, 1, SIMULATION_RATE, TRANSCRIBER_RATE, self.resample_state
        )
        self.audio_queue.put(converted)

    def cleanup(self):
        self.running = False
        print("🧹 Closing Hardware & Files...")
        try: self.file_handle.close()
        except: pass
        self.audio_queue.put(None) 
        if self.output_stream:
            self.output_stream.stop_stream()
            self.output_stream.close()
        self.p.terminate()

async def main():
    loop = asyncio.get_running_loop()
    bridge = STTBridge("P0001", loop)
    
    stt_thread = threading.Thread(target=bridge.start_transcription_loop, daemon=True)
    stt_thread.start()

    side_task = asyncio.create_task(bridge.side_process_worker())
    
    try:
        # Pass side_task to the server connector so it can cancel it
        await bridge.connect_to_server(side_task)
    except KeyboardInterrupt:
        print("\n🛑 Keyboard Stop")
    finally:
        bridge.cleanup()
        if not side_task.done():
            side_task.cancel()
        try:
            await side_task
        except asyncio.CancelledError:
            pass
        print("✨ Done.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())