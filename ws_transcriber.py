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
import time
from dotenv import load_dotenv
from google.cloud import speech
from google.generativeai import types

# Local Imports
import diagnosis_manager
import question_manager
import agents
import gcs_manager

load_dotenv()

# --- CONFIG UPDATED FOR LOCAL SERVER ---
# Changed from wss://... to ws://127.0.0.1:8000
WS_URL = "ws://127.0.0.1:8000/ws/simulation" 
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
        
        print(f"🚀 Starting LOCAL Session: {self.session_id} for Patient: {self.patient_id}")

        # --- Memory & Fast I/O ---
        self.full_transcript_memory = ""
        self.file_handle = open(OUTPUT_FILE, "w", encoding="utf-8", buffering=1)
        self.file_handle.write(f"--- SESSION {self.session_id} START ---\n\n")
        
        # --- Pre-initialize Agents ---
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
        
        # Fetch Patient Context
        gcs = gcs_manager.GCSManager()
        self.patient_info = gcs.read_text(f"patient_profile/{self.patient_id}/patient_info.md")

    def write_to_file_fast(self, text):
        self.file_handle.write(f"{text}\n")
        self.file_handle.flush()
        os.fsync(self.file_handle.fileno()) 
        self.full_transcript_memory += f"{text}\n"


    async def init_diagnosis_agents(self):
        print(f"⚙️  Initializing Diagnosis Agents...")
        h_task = self.hepa_agent.get_hepa_diagnosis("**INITIAL DIAGNOSIS FROM PATIENT INFO**", self.patient_info)
        g_task = self.gen_agent.get_gen_diagnosis("**INITIAL DIAGNOSIS FROM PATIENT INFO**", self.patient_info)
        hepa_diag, gen_diag = await asyncio.gather(h_task, g_task)
        consolidated = await self.consolidate_agent.consolidate_diagnosis(
                    self.dm.diagnoses, hepa_diag + gen_diag
                )
        self.dm.diagnoses = consolidated
        print(f"✅ Initial Diagnoses Loaded.")
        with open(f'output/consolidated_init.json', 'w', encoding='utf-8') as f:
            json.dump(consolidated, f, indent=4)

        ranked_questions = await self.merger_agent.process_question(
                "**INITIAL FROM PATIENT INFO**", consolidated, self.qm.get_questions_basic()
            )
        self.qm.add_questions(ranked_questions)
        next_q_obj = self.qm.get_high_rank_question()
        next_q_text = next_q_obj.get("content") if next_q_obj else None

        with open(f'status_update.json', 'w', encoding='utf-8') as f:
            json.dump({
                    "session_id": self.session_id, 
                    "is_finished": False,
                    "question": next_q_text
                }, f, indent=4)

    async def side_process_worker(self):
        print(f"⚙️  AI Side Processor Active.")
        count = 0
        try:
            while self.running:
                text = await self.transcript_queue.get()
                if text is None: break
                
                print(f"\n🧠 AI Processing Cycle {count}...")
                current_transcript = self.full_transcript_memory
                with open('side_transcript.txt', 'w', encoding='utf-8') as file:
                    file.write(current_transcript)
                # 1. Run Diagnoses
                h_task = self.hepa_agent.get_hepa_diagnosis(current_transcript, self.patient_info)
                g_task = self.gen_agent.get_gen_diagnosis(current_transcript, self.patient_info)
                hepa_diag, gen_diag = await asyncio.gather(h_task, g_task)
                print(f"\n🧠 Diagnosis {count}...")

                # 2. Consolidate
                consolidated = await self.consolidate_agent.consolidate_diagnosis(
                    self.dm.diagnoses, hepa_diag + gen_diag
                )
                self.dm.diagnoses = consolidated
                print(f"\n🧠 Consolidaate Diagnosis {count}...")
                
                os.makedirs('output', exist_ok=True)
                with open(f'output/consolidated_{count}.json', 'w', encoding='utf-8') as f:
                    json.dump(consolidated, f, indent=4)

                # 3. Check for Completion
                status = await self.supervisor.check_completion(current_transcript, consolidated)
                print(f"\n🧠 Supervisor Diagnosis {count}...")
                
                # 4. Handle Question Pool
                ranked_questions = await self.merger_agent.process_question(
                    current_transcript, consolidated, self.qm.get_questions_basic()
                )
                print(f"\n🧠 Question Diagnosis {count}...")

                self.qm.add_questions(ranked_questions)

                # 5. Output for Simulation Agent (Reads status_update.json)
                next_q_obj = self.qm.get_high_rank_question()
                next_q_text = next_q_obj.get("content") if next_q_obj else None

                with open(f'status_update.json', 'w', encoding='utf-8') as f:
                    json.dump({
                        "session_id": self.session_id, 
                        "is_finished": status.get("end", False),
                        "question": next_q_text
                    }, f, indent=4)
                
                print(f"✅ AI Cycle {count} Complete. Next Question: {next_q_text}")
                count += 1
                self.transcript_queue.task_done()
        except asyncio.CancelledError:
            pass

    def listen_print_loop(self, responses):
        for response in responses:
            if not self.running: break
            if not response.results: continue
            result = response.results[0]
            if not result.alternatives: continue
            transcript = result.alternatives[0].transcript

            if result.is_final:
                self.write_to_file_fast(transcript)
                sys.stdout.write(f"\r📝 Final: {transcript}\n")
                self.loop.call_soon_threadsafe(self.transcript_queue.put_nowait, transcript)
            else:
                # sys.stdout.write(f"\r📝 Interim: {transcript}...")
                sys.stdout.flush()

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
                self.listen_print_loop(responses)
            except Exception as e:
                if self.running: 
                    time.sleep(1)

    def audio_generator(self):
        while self.running:
            try:
                chunk = self.audio_queue.get(timeout=0.5)
                if chunk is None: return
                yield chunk
            except queue.Empty:
                continue

    async def connect_to_server(self, side_task):
        """Connects to LOCAL WebSocket server."""
        print(f"🔌 Connecting to LOCAL server: {WS_URL}")
        try:
            async with websockets.connect(WS_URL) as ws:
                # Same start message as your server.py expects
                await ws.send(json.dumps({
                    "type": "start", 
                    "patient_id": self.patient_id, 
                    "gender": "Male"
                }))
                
                while self.running:
                    msg = await ws.recv()
                    data = json.loads(msg) if isinstance(msg, str) else None
                    
                    # Watch for the 'end' turn signal from simulation.py
                    if data and data.get("type") == "turn" and data.get("data") == "end":
                        print("\n🏁 RECEIVED END SIGNAL. Shutting down...")
                        self.running = False
                        side_task.cancel()
                        self.audio_queue.put(None)
                        break

                    if data and data.get("type") == "audio":
                        await self.process_audio(base64.b64decode(data["data"]))
                    elif isinstance(msg, bytes):
                        await self.process_audio(msg)

        except Exception as e:
            print(f"❌ Local Connection Error: {e}")

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
        print("🧹 Cleaning up...")
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
    await bridge.init_diagnosis_agents()

    stt_thread = threading.Thread(target=bridge.start_transcription_loop, daemon=True)
    stt_thread.start()

    side_task = asyncio.create_task(bridge.side_process_worker())
    
    try:
        await bridge.connect_to_server(side_task)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.cleanup()
        if not side_task.done():
            side_task.cancel()
        print("✨ Local Session Closed.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())