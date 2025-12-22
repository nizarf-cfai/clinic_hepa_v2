# --- simulation.py ---
import asyncio
import threading
import copy
import json
import logging
import datetime
import contextlib
from fastapi import WebSocket

# Local Imports
import agents
import question_manager
import diagnosis_manager
from utils import fetch_gcs_text_internal

logger = logging.getLogger("medforce-backend")

# --- LOAD STATIC DATA ---
try:
    with open("questions.json", 'r') as file:
        QUESTION_LIST = json.load(file)
    with open("patient_profile/nurse.md", "r", encoding="utf-8") as f:
        NURSE_PROMPT = f.read()
except Exception as e:
    logger.error(f"Failed to load static files: {e}")
    QUESTION_LIST = []
    NURSE_PROMPT = "You are a nurse."

class TranscriptManager:
    def __init__(self):
        self.history = []
        self._lock = threading.Lock()
    
    def log(self, speaker, text, highlight_data=None):
        with self._lock:
            entry = {"timestamp": datetime.datetime.now().strftime("%H:%M:%S"), "speaker": speaker, "text": text.strip()}
            if speaker == "PATIENT": entry["highlight"] = highlight_data or []
            self.history.append(entry)
            logger.info(f"📝 {speaker}: {text[:50]}...")
    
    def get_history(self):
        with self._lock:
            return copy.deepcopy(self.history)

class ClinicalLogicThread(threading.Thread):
    def __init__(self, transcript_manager, qm, dm, shared_state, main_loop, websocket):
        super().__init__()
        self.tm = transcript_manager
        self.qm = qm
        self.dm = dm
        self.shared_state = shared_state
        self.main_loop = main_loop 
        self.websocket = websocket
        
        self.running = True
        self.daemon = True 
        self.last_processed_count = 0

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        self.trigger = agents.DiagnosisTriggerAgent()
        self.diagnoser = agents.DiagnoseAgent(patient_info=self.shared_state.get('patient_info'))
        self.evaluator = agents.DiagnoseEvaluatorAgent()
        self.ranker = agents.QuestionRankingAgent(patient_info=self.shared_state.get('patient_info'))

        logger.info("🩺 Logic Thread Started")
        loop.run_until_complete(self._monitor_loop())

    async def _push_update(self, type_str, data):
        if self.websocket and self.main_loop and not self.websocket.client_state.name == "DISCONNECTED":
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.websocket.send_json({"type": type_str, "data": data}),
                    self.main_loop
                )
                future.result(timeout=1)
            except Exception:
                pass

    async def _monitor_loop(self):
        while self.running:
            try:
                history = self.tm.get_history()
                current_len = len(history)

                if current_len > self.last_processed_count:
                    logger.info(f"⚡ New Transcript Detected ({current_len} turns). Running Logic...")
                    
                    # 1. Diagnose
                    diag_res = await self.diagnoser.get_diagnosis_update(history, self.dm.get_diagnosis_basic())
                    self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
                    
                    # 2. Evaluate
                    merged_diag = await self.evaluator.evaluate_diagnoses(
                        self.dm.get_consolidated_diagnoses_basic(),
                        diag_res.get("diagnosis_list"), 
                        history
                    )
                    self.dm.set_consolidated_diagnoses(merged_diag)
                    
                    # 3. Questions
                    self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
                    
                    # 4. Rank
                    diag_stream = self.dm.get_consolidated_diagnoses()
                    q_list = self.qm.get_recommend_question()
                    ranked_q = await self.ranker.rank_questions(history, diag_stream, q_list)
                    self.qm.update_ranking(ranked_q)

                    # 5. Push
                    await self._push_update("diagnosis", diag_stream)
                    await self._push_update("questions", self.qm.get_questions())
                    
                    self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
                    
                    self.last_processed_count = current_len
                    logger.info("✅ Logic Cycle Complete")

            except Exception as e:
                logger.error(f"Logic Thread Error: {e}")
            
            await asyncio.sleep(1)

    def stop(self):
        self.running = False

class SimulationManager:
    def __init__(self, websocket: WebSocket, patient_id: str, gender:str = "Male"):
        self.websocket = websocket
        
        self.PATIENT_PROMPT = fetch_gcs_text_internal(patient_id, "patient_system.md")
        self.PATIENT_INFO = fetch_gcs_text_internal(patient_id, "patient_info.md")

        # Voice Agents
        self.nurse = agents.TextBridgeAgent("NURSE", NURSE_PROMPT, "Aoede")
        if gender == "Male":
            self.patient = agents.TextBridgeAgent("PATIENT", self.PATIENT_PROMPT, "Puck")
        else:
            self.patient = agents.TextBridgeAgent("PATIENT", self.PATIENT_PROMPT, "Laomedeia")
        
        # Logic Agents
        self.advisor = agents.AdvisorAgent(patient_info=self.PATIENT_INFO)
        self.highlighter = agents.AnswerHighlighterAgent()
        self.diagnoser = agents.DiagnoseAgent(patient_info=self.PATIENT_INFO)
        self.evaluator = agents.DiagnoseEvaluatorAgent()
        self.ranker = agents.QuestionRankingAgent(patient_info=self.PATIENT_INFO)
        
        self.tm = TranscriptManager()
        self.qm = question_manager.QuestionPoolManager(copy.deepcopy(QUESTION_LIST))
        self.dm = diagnosis_manager.DiagnosisManager()
        
        self.cycle = 0
        self.shared_state = {
            "ranked_questions": self.qm.get_recommend_question(),
            "cycle": 0,
            "patient_info" : self.PATIENT_INFO
        }
        self.running = False

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        # --- INITIALIZATION PHASE ---
        try:
            logger.info("⚡ Running Initial Diagnosis (Main Thread)...")
            initial_history = [{"speaker": "PATIENT_INFO", "text": self.PATIENT_INFO}]

            diag_res = await self.diagnoser.get_diagnosis_update(initial_history, self.dm.get_diagnosis_basic())
            self.dm.update_diagnoses(diag_res.get("diagnosis_list"))
            
            merged_diag = await self.evaluator.evaluate_diagnoses(
                self.dm.get_consolidated_diagnoses_basic(),
                diag_res.get("diagnosis_list"), 
                initial_history
            )
            self.dm.set_consolidated_diagnoses(merged_diag)
            
            self.qm.add_questions_from_text(diag_res.get("follow_up_questions"))
            diag_stream = self.dm.get_consolidated_diagnoses()
            q_list = self.qm.get_recommend_question()
            
            ranked_q = await self.ranker.rank_questions(initial_history, diag_stream, q_list)
            self.qm.update_ranking(ranked_q)

            self.shared_state["ranked_questions"] = self.qm.get_recommend_question()
            await self.websocket.send_json({"type": "diagnosis", "data": diag_stream})
            await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})
            
            logger.info("✅ Init Logic Complete")

        except Exception as e:
            logger.error(f"Init Error: {e}")
            await self.websocket.send_json({"type": "system", "message": "Init Error, proceeding..."})

        # --- START BACKGROUND MONITORING ---
        self.logic_thread = ClinicalLogicThread(
            self.tm, self.qm, self.dm, self.shared_state, 
            asyncio.get_running_loop(), self.websocket
        )
        self.logic_thread.start()

        # --- START VOICE LOOPS ---
        async with contextlib.AsyncExitStack() as stack:
            self.nurse.set_session(await stack.enter_async_context(self.nurse.get_connection_context()))
            self.patient.set_session(await stack.enter_async_context(self.patient.get_connection_context()))
            await self.websocket.send_json({"type": "system", "message": "Starting Assessment."})

            next_instruction = "Intoduce yourself and tell the patient you have patient data and will asked further question for detailed health condition."
            patient_last_words = "Hello."
            interview_end = False
            last_qid = None
            
            while self.running:
                self.shared_state["cycle"] = self.cycle 

                # 1. NURSE
                nurse_input = f"Patient said: '{patient_last_words}'\n[SUPERVISOR: {next_instruction}]"
                nurse_text, _ = await self.nurse.speak_and_stream(nurse_input, self.websocket)
                
                if not nurse_text: nurse_text = "[The nurse waits]"
                self.tm.log("NURSE", nurse_text)

                await asyncio.sleep(0.5)
                await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})

                # 2. PATIENT
                current_diagnosis_context = self.dm.get_consolidated_diagnoses_basic()
                patient_text, highlight_result = await self.patient.speak_and_stream(
                    nurse_text, 
                    self.websocket, 
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
                    await self.websocket.send_json({"type": "questions", "data": self.qm.get_questions()})

                self.tm.log("PATIENT", patient_text, highlight_data=highlight_result)
                await asyncio.sleep(0.5)
                await self.websocket.send_json({"type": "turn", "data": "finish cycle"})
                if interview_end: break

                # 3. ADVISOR
                try:
                    current_ranked = self.shared_state["ranked_questions"]
                    question, reasoning, status, qid = await self.advisor.get_advise(self.tm.get_history(), current_ranked)
                    
                    if qid: 
                        self.qm.update_status(qid, "asked")
                        last_qid = qid
                    
                    await self.websocket.send_json({"type": "system", "message": f"Logic: {reasoning}"})
                    
                    next_instruction = question
                    interview_end = status
                    self.cycle += 1

                except Exception as e:
                    logger.error(f"Main Loop Logic Error: {e}")
                    next_instruction = "Continue assessment."

                if self.websocket.client_state.name == "DISCONNECTED": break

            await self.websocket.send_json({"type": "turn", "data": "end"})

        self.logic_thread.stop()