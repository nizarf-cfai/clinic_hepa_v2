import asyncio
import threading
import json
import audioop
import queue
import logging
import os
import time
import sys
import wave
from google.cloud import speech
from datetime import datetime
from utils import fetch_gcs_text_internal
import pyaudio

# Local Imports (Ensure these exist in your local path)
try:
    import agents
    import diagnosis_manager
    import question_manager
    import education_manager
except ImportError:
    # Fallback for local testing if modules aren't present
    print("Warning: Agent modules not found. Ensure agents.py, etc., are in the directory.")

logger = logging.getLogger("medforce-backend")
TRANSCRIPT_FILE = "simulation_transcript.txt"

# --- MODULAR AUDIO SOURCES ---

class BaseAudioSource:
    """Base class for audio input."""
    def get_requests(self):
        raise NotImplementedError

class FileAudioSource(BaseAudioSource):
    """Streams a local WAV file to the transcriber and plays it locally."""
    def __init__(self, file_path, chunk_size=2000, target_rate=16000, play_locally=True):
        self.file_path = file_path
        self.chunk_size = chunk_size
        self.target_rate = target_rate
        self.play_locally = play_locally
        self.p = pyaudio.PyAudio() if play_locally else None

    def get_requests(self):
        with wave.open(self.file_path, 'rb') as wf:
            source_rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            channels = wf.getnchannels()
            
            # Setup local playback stream
            stream = None
            if self.play_locally:
                stream = self.p.open(
                    format=self.p.get_format_from_width(sampwidth),
                    channels=channels,
                    rate=source_rate,
                    output=True
                )

            # Calculate sleep time to simulate real-time playback
            sleep_duration = (self.chunk_size / (channels * sampwidth)) / source_rate
            
            resample_state = None
            print(f"🔊 [Audio] Starting playback and stream: {self.file_path}")

            try:
                while True:
                    # Read original raw data
                    original_data = wf.readframes(self.chunk_size // (channels * sampwidth))
                    if not original_data:
                        break
                    
                    # 1. PLAY LOCALLY (High Quality/Original)
                    if stream:
                        stream.write(original_data)
                    
                    # 2. PREPARE FOR STT (Downsample/Mono)
                    data_to_process = original_data
                    if channels > 1:
                        data_to_process = audioop.tomono(data_to_process, sampwidth, 1, 1)
                    
                    converted, resample_state = audioop.ratecv(
                        data_to_process, sampwidth, 1, source_rate, self.target_rate, resample_state
                    )
                    
                    # Yield to Google STT
                    yield speech.StreamingRecognizeRequest(audio_content=converted)
                    
                    # Note: We don't need a heavy sleep anymore because stream.write() 
                    # is "blocking" and naturally handles the timing.
            finally:
                if stream:
                    stream.stop_stream()
                    stream.close()
                if self.p:
                    self.p.terminate()

class QueueAudioSource(BaseAudioSource):
    """Streams audio from a live queue (original simulation behavior)."""
    def __init__(self, audio_queue, delay_sec=4.0):
        self.audio_queue = audio_queue
        self.delay_sec = delay_sec

    def get_requests(self):
        while True:
            item = self.audio_queue.get()
            if item is None: break
            
            release_time, chunk = item
            now = time.time()
            if now < release_time:
                time.sleep(release_time - now)
            
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

# --- REFACTORED LOGIC THREAD ---

class TranscriberLogicThread(threading.Thread):
    def __init__(self, patient_info, dm, qm, main_loop=None, websocket=None):
        super().__init__()
        self.patient_info = patient_info
        self.dm = dm
        self.qm = qm
        self.main_loop = main_loop 
        self.websocket = websocket
        self.running = True
        self.daemon = True 
        self.qc = agents.QuestionCheck()
        self.last_line_count = 0 
        self.ready_event = threading.Event()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        self.hepa_agent = agents.DiagnosisHepato()
        self.gen_agent = agents.DiagnosisGeneral()
        self.consolidate_agent = agents.DiagnosisConsolidate()
        self.merger_agent = agents.QuestionMerger()
        self.supervisor = agents.InterviewSupervisor()
        self.transcript_parser = agents.TranscribeStructureAgent()
        self.q_enrich = agents.QuestionEnrichmentAgent()
        self.analytics_agent = agents.ConsultationAnalyticAgent()
        self.education_agent = agents.PatientEducationAgent()
        self.em = education_manager.EducationPoolManager()



        self.transcript_structure = []
        self.education_list = []

        with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
            f.write("")

        print(f"🩺 [Logic Thread] Monitoring {TRANSCRIPT_FILE}...")
        loop.run_until_complete(self.start_logic())

    async def start_logic(self):
        await self.run_initial_analysis()
        print("🔔 [Logic Thread] Initial analysis complete. Signaling STT to start...")
        self.ready_event.set()  # <--- Trigger the signal
        
        await self._logic_loop()


    async def run_initial_analysis(self):
        print(f"🩺 [Logic Thread] Initial Analysis Starting...")
        await self._push_to_ui({
            "type": "status",
            "data": {
                    "end": False,
                    "state": "initiate"
                },
            "source" : "logic_check"
        })

        initial_instruction = "Initial file review."
        h_coro = self.hepa_agent.get_hepa_diagnosis(initial_instruction, self.patient_info)
        g_coro = self.gen_agent.get_gen_diagnosis(initial_instruction, self.patient_info)
        
        hepa_res, gen_res = await asyncio.gather(h_coro, g_coro)
        consolidated = await self.consolidate_agent.consolidate_diagnosis(self.dm.diagnoses, hepa_res + gen_res)
        self.dm.diagnoses = consolidated

        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses(),
            "source" : "initial_analysis"
        })

        # await self._save_json({
        #     "type": "diagnosis",
        #     "diagnosis": self.dm.get_diagnoses()
        # }, "initial_diagnosis.json")

        ranked_questions = await self.merger_agent.process_question("", consolidated, self.qm.get_questions_basic())
        self.qm.add_questions(ranked_questions)

        enriched_q = await self.q_enrich.enrich_questions(self.qm.get_questions_basic())
        self.qm.update_enriched_questions(enriched_q)
        
        await self._push_to_ui({"type": "questions", "questions": self.qm.questions, "source" : "initial_analysis"})

        # await self._save_json({"type": "questions", "questions": self.qm.questions, "source" : "initial_analysis"}, "initial_questions.json")

        print(f"🩺 [Logic Thread] Initial Analysis Finished.")

    async def _save_json(self, data, filename):
        with open("output/" + filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    async def _push_to_ui(self, payload, save_locally=True):
        # Only attempt send if websocket and loop exist
        if save_locally:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            await self._save_json(payload, f"{payload.get('type','data')}_{payload.get('source','data')}_{timestamp}.json")

        if self.websocket and self.main_loop:
            try:
                asyncio.run_coroutine_threadsafe(self.websocket.send_json(payload), self.main_loop)
            except Exception: pass
        else:
            # Local console output if no UI
            print(f"🖥️ [UI Update]: {payload.get('type')}")
            


    async def _check_q(self, transcript, question_pool):
        print(f"🩺 [Logic Thread] Checking Questions...")
        eduaction_task = self.education_agent.generate_education(self.transcript_structure, self.education_list)

        answered_list_task = self.qc.check_question(transcript, question_pool)
        enriched_q_task =  self.q_enrich.enrich_questions(self.qm.get_questions_basic())
        status_task =  self.supervisor.check_completion(transcript, self.dm.diagnoses)


        answered_list, enriched_q_res, status, eduaction_task_res = await asyncio.gather(
                        answered_list_task,
                        enriched_q_task, 
                        status_task,
                        eduaction_task
                        )
        

        for aq in answered_list:
            print(f"✅ [Question Check] QID {aq['qid']} answered with: {aq['answer']}")
            self.qm.update_status(aq['qid'], "asked")
            self.qm.update_answer(aq['qid'], aq['answer'])

        print("ENRICHED Q RES:", enriched_q_res)

        self.qm.update_enriched_questions(enriched_q_res)


        next_q_obj = self.qm.get_high_rank_question()
        next_q_text = next_q_obj.get("content") if next_q_obj else None

        await self._push_to_ui({
            "type": "education",
            "data": self.em.pool,
            "source" : "logic_check"
        })

        
        
        await self._push_to_ui({
            "type": "status",
            "data": status,
            "source" : "logic_check"
        })
        self.em.add_new_points(eduaction_task_res)
        print("Education result:\n", eduaction_task_res)

        print("Education Pool:\n", self.em.pool)
        next_ed = self.em.pick_and_mark_asked()
        if next_ed:
            next_ed_content = next_ed.get("content","")
            print(f"🆕 [Education] Next Education Point: {next_ed_content}")
        else:
            next_ed_content = ""

        with open('status_update.json', 'w', encoding='utf-8') as f:
            json.dump({
                "is_finished": status.get("end", False),
                "question": next_q_text,
                "education" : next_ed_content
            }, f, indent=4)

        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses(),
            "source" : "logic_check"

        })

        # await self._save_json({
        #     "type": "diagnosis",
        #     "diagnosis": self.dm.get_diagnoses()
        # }, f"diagnosis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

        await self._push_to_ui({
            "type": "questions",
            "questions": self.qm.questions,
            "source" : "logic_check"
        })

        # await self._save_json({"type": "questions", "questions": self.qm.questions, "source" : "initial_analysis"}, f"questions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")



        print(f"🩺 [Logic Thread] Checking Questions Finished.")

    async def _logic_loop(self):
        while self.running:
            try:
                if not os.path.exists(TRANSCRIPT_FILE):
                    await asyncio.sleep(1)
                    continue

                with open(TRANSCRIPT_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    

                if len(lines) > self.last_line_count:
                    new_transcript_text = " ".join([l.strip() for l in lines if l.strip()])
                    print(f"\n🤖 [AI Agent] Processing: {new_transcript_text[:100]}...")

                    await self._check_q(new_transcript_text, self.qm.get_questions_basic())
                    
                    # 1. Run Diagnoses
                    # eduaction_task = self.education_agent.generate_education(self.transcript_structure, self.education_list)
                    analytics_task = self.analytics_agent.analyze_consultation(self.transcript_structure)
                    structure_transcript_task = self.transcript_parser.structure_transcription(self.transcript_structure, new_transcript_text)
                    h_task = self.hepa_agent.get_hepa_diagnosis(new_transcript_text, self.patient_info)
                    g_task = self.gen_agent.get_gen_diagnosis(new_transcript_text, self.patient_info)


                    hepa_res, gen_res, self.transcript_structure, analytics_task_res = await asyncio.gather(
                        h_task,
                        g_task, 
                        structure_transcript_task,
                        analytics_task,
                        )
                    
                    

                    await self._push_to_ui({
                        "type": "chat",
                        "data": self.transcript_structure,
                        "source" : "logic_loop"
                    })


                    await self._push_to_ui({
                        "type": "analytics",
                        "data": analytics_task_res,
                        "source" : "logic_loop"
                    })
                    


                    # 2. Consolidate
                    consolidated = await self.consolidate_agent.consolidate_diagnosis(
                        self.dm.diagnoses, hepa_res + gen_res
                    )
                    self.dm.diagnoses = consolidated

                    # 3. Check Status & Questions
                    ranked_questions = await self.merger_agent.process_question(
                        new_transcript_text, consolidated, self.qm.get_questions_basic()
                    )
                    self.qm.add_questions(ranked_questions)
                    
                    self.last_line_count = len(lines)
                    await asyncio.sleep(5) 
                    print(f"\n🤖 [AI Agent] Processing Finished")

                else:
                    await asyncio.sleep(1)
            except Exception as e:
                print(f"❌ [Logic Thread] Error: {e}")
                await asyncio.sleep(2)

        await self._push_to_ui({
            "type": "status",
            "data": {
                    "end": True,
                    "state": "finished"
                },
            "source" : "logic_check"
        })

    def stop(self):
        self.running = False

# --- REFACTORED ENGINE ---

class TranscriberEngine:
    def __init__(self, patient_info, audio_source: BaseAudioSource, websocket=None, loop=None):
        self.websocket = websocket
        self.patient_info = patient_info
        self.main_loop = loop
        self.audio_source = audio_source
        self.running = True
        self.TRANSCRIBER_RATE = 16000


        # Initialize Logic Thread
        self.logic_thread = TranscriberLogicThread(
            self.patient_info, 
            diagnosis_manager.DiagnosisManager(), 
            question_manager.QuestionPoolManager([]), 
            self.main_loop, self.websocket
        )
        self.logic_thread.start()

    def wait_for_ready(self):
        """Blocks the main thread until the logic thread finishes initial analysis."""
        print("⏳ [Engine] Waiting for initial analysis to complete...")
        self.logic_thread.ready_event.wait() 
        print("🚀 [Engine] Ready! Starting audio playback and transcription.")

    def stt_loop(self):
        client = speech.SpeechClient()
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.TRANSCRIBER_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_long",
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=True)

        print(f"🎙️ [STT Loop] Starting transcription via {type(self.audio_source).__name__}")

        while self.running:
            try:
                responses = client.streaming_recognize(
                    streaming_config, 
                    self.audio_source.get_requests()
                )
                
                for response in responses:
                    if not self.running: break
                    if not response.results: continue
                    
                    result = response.results[0]
                    transcript = result.alternatives[0].transcript

                    if result.is_final:
                        print(f"\n🎙️ [STT FINAL]: {transcript}")
                        
                        # Use a simpler delayed write for modularity
                        def delayed_write(text):
                            with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
                                f.write(text + "\n")
                                f.flush()

                        threading.Thread(target=delayed_write, args=(transcript,), daemon=True).start()
                        
            except Exception as e:
                if self.running:
                    print(f"🎙️ [STT Connection Reset/Error]: {e}")
                time.sleep(1)


    def stop(self):
        self.running = False
        self.logic_thread.stop()

# --- LOCAL RUNNER EXAMPLE ---

if __name__ == "__main__":
    # Example for running locally with a file
    # mock_patient = {"patient_id":"P0001","name": "John Doe", "age": 45, "history": "None"}
    
    # 1. Choose your source
    file_source = FileAudioSource("simulation_final.wav")
    patient_info = fetch_gcs_text_internal("P0001", "patient_info.md")
    
    # 2. Init Engine (Websocket=None for local)
    engine = TranscriberEngine(patient_info, file_source)
    
    try:
        engine.wait_for_ready() 
        engine.stt_loop()
    except KeyboardInterrupt:
        engine.stop()
        print("Stopped.")