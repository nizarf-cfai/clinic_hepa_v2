import asyncio
import os
import json
import logging
import datetime
import threading
import copy
import sys
import contextlib
import wave  # Required for WAV file handling
from dotenv import load_dotenv

# Audio
import pyaudio

# Google SDKs
from google import genai
from google.genai import types
from google.cloud import storage

# Local Modules
import question_manager
import diagnosis_manager

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
# Silence internal SDK logs
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("medforce-local")

load_dotenv()

# --- Configuration ---
FORMAT = pyaudio.paInt16
CHANNELS = 1
GEMINI_OUTPUT_RATE = 24000 # Gemini Native Rate
VOICE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
ADVISOR_MODEL = "gemini-2.5-flash-lite" 
DIAGNOSER_MODEL = "gemini-2.5-flash-lite" 
RANKER_MODEL = "gemini-2.5-flash-lite" 
BUCKET_NAME = "clinic_sim"
OUTPUT_DIR = "output" 

# Initialize PyAudio
pya = pyaudio.PyAudio()

# Ensure output directory exists
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- Load Static Data ---
try:
    with open("questions.json", 'r') as file:
        QUESTION_LIST = json.load(file)
    with open("patient_profile/nurse.md", "r", encoding="utf-8") as f:
        NURSE_PROMPT = f.read()
except Exception as e:
    logger.error(f"Failed to load local static files: {e}")
    QUESTION_LIST = []
    NURSE_PROMPT = "You are a nurse."

# ==========================================
# UTILS
# ==========================================

def fetch_gcs_text_internal(patient_id, filename):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_path = f"patient_profile/{patient_id}/{filename}"
        blob = bucket.blob(blob_path)
        
        if not blob.exists():
            logger.error(f"❌ GCS File not found: {blob_path}")
            return "Info not available."
            
        content = blob.download_as_text()
        logger.info(f"📥 Fetched GCS: {blob_path}")
        return content
    except Exception as e:
        logger.error(f"❌ GCS Error: {e}")
        return "Error loading info."

# ==========================================
# LOGIC AGENTS (Identical to Server)
# ==========================================

class BaseLogicAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))

class QuestionRankingAgent(BaseLogicAgent):
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"rank": { "type": "INTEGER" }, "qid": { "type": "STRING" }}, "required": ["rank", "qid"]}}
        self.patient_info = patient_info
        try:
            with open("patient_profile/q_ranker.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Rank by priority."

    async def rank_questions(self, conversation_history, current_diagnosis, q_list):
        prompt = f"Patient Profile:\n{self.patient_info}\n\nHistory:\n{json.dumps(conversation_history)}\n\nDiagnosis:\n{json.dumps(current_diagnosis)}\n\nQuestions:\n{json.dumps(q_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=RANKER_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.1)
            )
            return json.loads(response.text)
        except Exception:
            return [{"rank": i+1, "qid": q["qid"]} for i, q in enumerate(q_list)]

class DiagnosisTriggerAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {"type": "OBJECT", "properties": {"should_run": { "type": "BOOLEAN" }, "reason": { "type": "STRING" }}, "required": ["should_run", "reason"]}
        try:
            with open("patient_profile/diagnosis_trigger.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def check_trigger(self, conversation_history):
        if not conversation_history: return False, "Empty"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=f"History:\n{json.dumps(conversation_history)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.0)
            )
            res = json.loads(response.text)
            return res.get("should_run", False), res.get("reason", "")
        except: return True, "Fallback"

class DiagnoseEvaluatorAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"diagnosis": { "type": "STRING" }, "did": { "type": "STRING" }, "indicators_point": { "type": "ARRAY", "items": { "type": "STRING" } }}, "required": ["diagnosis", "did", "indicators_point"]}}
        try:
            with open("patient_profile/diagnosis_eval.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Merge diagnoses."

    async def evaluate_diagnoses(self, diagnosis_pool, new_diagnosis_list, interview_data):
        prompt = f"Context:\n{json.dumps(interview_data)}\n\nMaster Pool:\n{json.dumps(diagnosis_pool)}\n\nNew Candidates:\n{json.dumps(new_diagnosis_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.1)
            )
            return json.loads(response.text)
        except: return diagnosis_pool + new_diagnosis_list

class DiagnoseAgent(BaseLogicAgent):
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "OBJECT", "properties": {"diagnosis_list": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"diagnosis": { "type": "STRING" }, "did": { "type": "STRING" }, "indicators_point": { "type": "ARRAY", "items": { "type": "STRING" } }}, "required": ["diagnosis", "indicators_point", "did"]}}, "follow_up_questions": {"type": "ARRAY", "items": { "type": "STRING" }}}, "required": ["diagnosis_list", "follow_up_questions"]}
        self.patient_info = patient_info
        try:
            with open("patient_profile/diagnoser.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Diagnose patient."

    async def get_diagnosis_update(self, interview_data, current_diagnosis_hypothesis):
        prompt = f"Patient:\n{self.patient_info}\n\nTranscript:\n{json.dumps(interview_data)}\n\nState:\n{json.dumps(current_diagnosis_hypothesis)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=DIAGNOSER_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.2)
            )
            res = json.loads(response.text)
            return {"diagnosis_list": res.get("diagnosis_list", []), "follow_up_questions": res.get("follow_up_questions", [])}
        except: return {"diagnosis_list": current_diagnosis_hypothesis, "follow_up_questions": []}

class AdvisorAgent(BaseLogicAgent):
    def __init__(self, patient_info):
        super().__init__()
        self.response_schema = {"type": "OBJECT", "properties": {"question": { "type": "STRING" }, "qid": { "type": "STRING" }, "end_conversation": { "type": "BOOLEAN" }, "reasoning": { "type": "STRING" }}, "required": ["question", "end_conversation", "reasoning", "qid"]}
        self.patient_info = patient_info
        try:
            with open("patient_profile/advisor_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Advise nurse."

    async def get_advise(self, conversation_history, q_list):
        prompt = f"Context:\n{self.patient_info}\n\nHistory:\n{json.dumps(conversation_history)}\n\nQuestions:\n{json.dumps(q_list)}"
        try:
            response = await self.client.aio.models.generate_content(
                model=ADVISOR_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.2)
            )
            res = json.loads(response.text)
            return res.get("question"), res.get("reasoning"), res.get("end_conversation"), res.get("qid")
        except: return "Continue.", "Error", False, None

class AnswerHighlighterAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"level": { "type": "STRING", "enum": ["danger", "warning"] }, "text": { "type": "STRING" }}, "required": ["level", "text"]}}
        try:
            with open("patient_profile/highlight_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Extract keywords."

    async def highlight_text(self, patient_answer: str, diagnosis_list: list):
        if not patient_answer or len(patient_answer) < 3: return []
        prompt = f"Context:\n{json.dumps(diagnosis_list)}\n\nAnswer:\n\"{patient_answer}\""
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=self.response_schema, system_instruction=self.system_instruction, temperature=0.0)
            )
            return json.loads(response.text)
        except: return []

# ==========================================
# TRANSCRIPT & LOGIC
# ==========================================

class TranscriptManager:
    def __init__(self):
        self.history = []
        self._lock = threading.Lock()
    
    def log(self, speaker, text, highlight_data=None):
        with self._lock:
            entry = {"timestamp": datetime.datetime.now().strftime("%H:%M:%S"), "speaker": speaker, "text": text.strip()}
            if speaker == "PATIENT": 
                entry["highlight"] = highlight_data or []
            
            self.history.append(entry)
            print(f"\n💬 {speaker}: {text.strip()}")
            if highlight_data:
                print(f"   🔍 Highlights: {highlight_data}")
    
    def get_history(self):
        with self._lock:
            return copy.deepcopy(self.history)

class ClinicalLogicThread(threading.Thread):
    def __init__(self, transcript_manager, qm, dm, shared_state):
        super().__init__()
        self.tm = transcript_manager
        self.qm = qm
        self.dm = dm
        self.shared_state = shared_state
        self.running = True
        self.daemon = True 
        self.last_processed_count = 0

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        patient_info = self.shared_state.get('patient_info', "")
        self.trigger = DiagnosisTriggerAgent()
        self.diagnoser = DiagnoseAgent(patient_info)
        self.evaluator = DiagnoseEvaluatorAgent()
        self.ranker = QuestionRankingAgent(patient_info)

        print("⚙️  Logic Thread Started (Background)")
        loop.run_until_complete(self._monitor_loop())

    async def _monitor_loop(self):
        try:
            print("⚡ Running Initial Diagnosis...")
            init_hist = [{"speaker": "PATIENT_INFO", "text": self.shared_state.get('patient_info')}]
            
            diag_res = await self.diagnoser.get_diagnosis_update(init_hist, self.dm.get_diagnosis_basic())
            self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
            
            merged_diag = await self.evaluator.evaluate_diagnoses(self.dm.get_consolidated_diagnoses_basic(), diag_res.get("diagnosis_list"), init_hist)
            self.dm.set_consolidated_diagnoses(merged_diag)
            
            self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
            
            diag_stream = self.dm.get_consolidated_diagnoses()
            q_list = self.qm.get_recommend_question()
            
            ranked_q = await self.ranker.rank_questions(init_hist, diag_stream, q_list)
            self.qm.update_ranking(ranked_q)
            
            self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
            print("✅ Init Logic Complete.")

        except Exception as e:
            print(f"❌ Init Logic Error: {e}")

        while self.running:
            try:
                history = self.tm.get_history()
                current_len = len(history)

                if current_len > self.last_processed_count:
                    should_run, reason = await self.trigger.check_trigger(history)

                    if should_run:
                        diag_res = await self.diagnoser.get_diagnosis_update(history, self.dm.get_diagnosis_basic())
                        self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
                        merged_diag = await self.evaluator.evaluate_diagnoses(self.dm.get_consolidated_diagnoses_basic(), diag_res.get("diagnosis_list"), history)
                        self.dm.set_consolidated_diagnoses(merged_diag)
                        self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
                        diag_stream = self.dm.get_consolidated_diagnoses()
                        q_list = self.qm.get_recommend_question()
                        ranked_q = await self.ranker.rank_questions(history, diag_stream, q_list)
                        self.qm.update_ranking(ranked_q)
                        self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
                        print("\n✅ Logic Updated.")
                        if diag_stream:
                            print(f"   -> Top Diagnosis: {diag_stream[0].get('diagnosis')}")
                    
                    self.last_processed_count = current_len
            except Exception as e:
                print(f"Logic Error: {e}")
            
            await asyncio.sleep(2)

    def stop(self):
        self.running = False

# ==========================================
# LOCAL VOICE AGENT
# ==========================================

class LocalVoiceAgent:
    def __init__(self, name, system_instruction, voice_name, audio_stream, recording_buffer=None):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self.audio_stream = audio_stream 
        self.recording_buffer = recording_buffer
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))
        self.session = None

    def get_connection_context(self):
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"], 
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name))),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        return self.client.aio.live.connect(model=VOICE_MODEL, config=config)

    def set_session(self, session):
        self.session = session

    async def speak(self, text_input, highlighter=None, diagnosis_context=None):
        if not self.session: return None, []
        
        sys.stdout.write(f"\n💬 {self.name}: ")
        sys.stdout.flush()

        await self.session.send(input=text_input, end_of_turn=True)
        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                # 1. Play Audio & Record
                if data := response.data:
                    if self.audio_stream:
                        await asyncio.to_thread(self.audio_stream.write, data)
                    
                    if self.recording_buffer is not None:
                        self.recording_buffer.extend(data)

                # 2. Text
                if response.server_content and response.server_content.output_transcription:
                    if text := response.server_content.output_transcription.text:
                        text_accumulator.append(text)
                        sys.stdout.write(text)
                        sys.stdout.flush()

                # 3. Complete
                if response.server_content and response.server_content.turn_complete:
                    print("") 
                    full_text = "".join(text_accumulator).strip()
                    if full_text:
                        highlights = []
                        if highlighter and diagnosis_context:
                            try:
                                highlights = await highlighter.highlight_text(full_text, diagnosis_context)
                            except: pass
                        return full_text, highlights
                    return "[...]", []
            return None, []
        except Exception as e:
            print(f"\nError ({self.name}): {e}")
            return None, []

# ==========================================
# MAIN SIMULATION (With Incremental Saving)
# ==========================================

class LocalSimulation:
    def __init__(self, patient_id="P0001"):
        self.patient_id = patient_id
        
        print(f"🌍 Connecting to GCS for Patient: {patient_id}")
        self.PATIENT_PROMPT = fetch_gcs_text_internal(patient_id, "patient_system.md")
        self.PATIENT_INFO = fetch_gcs_text_internal(patient_id, "patient_info.md")

        # Local Audio Output
        self.stream = pya.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=GEMINI_OUTPUT_RATE,
            output=True
        )

        # Audio Buffer & File
        self.audio_buffer = bytearray()
        self.audio_filename = f"{OUTPUT_DIR}/simulation_inprogress.wav"

        # Voice Agents
        self.nurse = LocalVoiceAgent("NURSE", NURSE_PROMPT, "Aoede", self.stream, recording_buffer=self.audio_buffer)
        self.patient = LocalVoiceAgent("PATIENT", self.PATIENT_PROMPT, "Puck", self.stream, recording_buffer=self.audio_buffer)
        
        # Logic
        self.advisor = AdvisorAgent(patient_info=self.PATIENT_INFO)
        self.highlighter = AnswerHighlighterAgent()
        self.tm = TranscriptManager()
        self.qm = question_manager.QuestionPoolManager(copy.deepcopy(QUESTION_LIST))
        self.dm = diagnosis_manager.DiagnosisManager()
        
        self.cycle = 0
        self.shared_state = {
            "ranked_questions": self.qm.get_recommend_question(),
            "patient_info" : self.PATIENT_INFO
        }

    def add_recording_silence(self, seconds):
        if seconds <= 0: return
        num_bytes = int(GEMINI_OUTPUT_RATE * CHANNELS * 2 * seconds)
        silence = b'\x00' * num_bytes
        self.audio_buffer.extend(silence)

    def save_audio_snapshot(self):
        """
        Dumps the current audio buffer to disk. 
        Because WAV headers contain file size, we simply overwrite the file 
        with the latest cumulative data to ensure it's playable.
        """
        try:
            with wave.open(self.audio_filename, 'wb') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(pya.get_sample_size(FORMAT))
                wf.setframerate(GEMINI_OUTPUT_RATE)
                wf.writeframes(self.audio_buffer)
            # print("  (Audio Saved)") # Uncomment for debug
        except Exception as e:
            print(f"⚠️ Warning: Could not save audio snapshot: {e}")

    def save_final_data(self):
        print("\n💾 Saving Final Data...")
        try:
            with open(f"{OUTPUT_DIR}/questions_final.json", 'w', encoding='utf-8') as f:
                json.dump(self.qm.questions, f, indent=4)
            with open(f"{OUTPUT_DIR}/diagnosis_final.json", 'w', encoding='utf-8') as f:
                json.dump(self.dm.get_consolidated_diagnoses(), f, indent=4)
            with open(f"{OUTPUT_DIR}/transcript_final.json", 'w', encoding='utf-8') as f:
                json.dump(self.tm.get_history(), f, indent=4, ensure_ascii=False)
            
            # Rename partial audio to final
            if os.path.exists(self.audio_filename):
                final_audio = f"{OUTPUT_DIR}/simulation_final.wav"
                if os.path.exists(final_audio): os.remove(final_audio)
                os.rename(self.audio_filename, final_audio)
                print(f"✅ Audio saved to '{final_audio}'")
            
            print(f"✅ Data saved to '{OUTPUT_DIR}/' folder.")
        except Exception as e:
            print(f"❌ Error saving data: {e}")

    async def run(self):
        print("🤖 STARTING LOCAL SIMULATION (Recording Enabled)")
        print(f"🔴 Recording to: {self.audio_filename}")
        print("---------------------------------------")

        logic_thread = ClinicalLogicThread(self.tm, self.qm, self.dm, self.shared_state)
        logic_thread.start()

        async with contextlib.AsyncExitStack() as stack:
            self.nurse.set_session(await stack.enter_async_context(self.nurse.get_connection_context()))
            self.patient.set_session(await stack.enter_async_context(self.patient.get_connection_context()))
            
            print("✅ Agents Connected. Starting Conversation.")
            
            # Create the initial file
            self.save_audio_snapshot()

            next_instruction = "Intoduce yourself and tell the patient you have patient data and will asked further question for detailed health condition."
            patient_last_words = "Hello."
            interview_end = False
            last_qid = None
            
            while True:
                # 1. NURSE
                nurse_input = f"Patient said: '{patient_last_words}'\n[SUPERVISOR: {next_instruction}]"
                nurse_text, _ = await self.nurse.speak(nurse_input)
                
                if not nurse_text: nurse_text = "[The nurse waits]"
                self.tm.log("NURSE", nurse_text)

                # > Save Audio Snapshot
                self.save_audio_snapshot()

                self.add_recording_silence(0.5)
                await asyncio.sleep(0.5)

                # > Save Audio Snapshot (Include silence)
                self.save_audio_snapshot()

                # 2. PATIENT
                current_diagnosis_context = self.dm.get_consolidated_diagnoses_basic()
                patient_text, highlight_result = await self.patient.speak(
                    nurse_text, 
                    highlighter=self.highlighter, 
                    diagnosis_context=current_diagnosis_context
                )
                
                if patient_text:
                    patient_last_words = patient_text
                else:
                    patient_text = "[The patient nods]"
                    patient_last_words = "(Silent)"

                if last_qid:
                    self.qm.update_answer(last_qid, patient_text)

                self.tm.log("PATIENT", patient_text, highlight_data=highlight_result)
                
                # > Save Audio Snapshot
                self.save_audio_snapshot()

                self.add_recording_silence(0.5)
                await asyncio.sleep(0.5)
                
                # > Save Audio Snapshot
                self.save_audio_snapshot()

                if interview_end: break

                # 3. ADVISOR
                try:
                    current_ranked = self.shared_state["ranked_questions"]
                    question, reasoning, status, qid = await self.advisor.get_advise(self.tm.get_history(), current_ranked)
                    
                    if qid: 
                        self.qm.update_status(qid, "asked")
                        last_qid = qid
                    
                    print(f"\n🧠 Advisor Logic: {reasoning}")
                    print(f"👉 Next Instruction: {question}")
                    
                    next_instruction = question
                    interview_end = status
                    self.cycle += 1

                except Exception as e:
                    print(f"Advisor Error: {e}")
                    next_instruction = "Continue assessment."

            self.save_final_data()

        logic_thread.stop()
        self.stream.close()

def main():
    sim = LocalSimulation(patient_id="P0001")
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by User")
        sim.save_final_data()
    finally:
        pya.terminate()

if __name__ == "__main__":
    main()