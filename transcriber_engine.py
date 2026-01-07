import asyncio
import threading
import json
import audioop
import queue
import logging
import os
import time
from google.cloud import speech
from datetime import datetime
import traceback
# Local Imports
import agents
import diagnosis_manager
import question_manager
import education_manager

logger = logging.getLogger("medforce-backend")
TRANSCRIPT_FILE = "simulation_transcript.txt"

class TranscriberLogicThread(threading.Thread):
    def __init__(self, patient_info, dm, qm, main_loop, websocket, transcript_memory,run_status):
        super().__init__()
        self.patient_info = patient_info
        self.dm = dm
        self.qm = qm
        self.main_loop = main_loop 
        self.websocket = websocket
        self.running = run_status
        self.daemon = True 
        self.status = False
        self.transcript_memory = transcript_memory

        # Logic Components
        self.qc = agents.QuestionCheck()
        self.em = education_manager.EducationPoolManager()
        self.last_line_count = 0 
        self.ready_event = threading.Event()
        
        # Chat State
        self.transcript_structure = []
        self.analytics_pool = {}


    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Initialize Agents
        self.hepa_agent = agents.DiagnosisHepato()
        self.gen_agent = agents.DiagnosisGeneral()
        self.consolidate_agent = agents.DiagnosisConsolidate()
        self.merger_agent = agents.QuestionMerger()
        self.supervisor = agents.InterviewSupervisor()
        self.transcript_parser = agents.TranscribeStructureAgent()
        self.q_enrich = agents.QuestionEnrichmentAgent()
        self.analytics_agent = agents.ConsultationAnalyticAgent()
        self.education_agent = agents.PatientEducationAgent()
        self.ranker = agents.QuestionRanker()

        self.checklist_agent = agents.ClinicalChecklistAgent()
        self.report_agent = agents.ComprehensiveReportAgent()

        # Clear transcript file
        with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
            f.write("")

        logger.info(f"🩺 [Logic Thread] Monitoring {TRANSCRIPT_FILE}...")
        loop.run_until_complete(self.start_logic())

    

    async def start_logic(self):
        """Pre-analysis before allowing STT to process audio."""
        await self.run_initial_analysis()
        logger.info("🔔 [Logic Thread] Initial analysis complete. Signaling STT to start...")
        self.ready_event.set() 
        await self._logic_loop()

    async def run_initial_analysis(self):
        await self._push_to_ui({"type": "status", "data": {"end": False, "state": "initiate"}})

        initial_instruction = "Initial file review and patient history analysis."
        h_coro = self.hepa_agent.get_hepa_diagnosis(initial_instruction, self.patient_info)
        g_coro = self.gen_agent.get_gen_diagnosis(initial_instruction, self.patient_info)
        
        hepa_res, gen_res = await asyncio.gather(h_coro, g_coro)
        consolidated = await self.consolidate_agent.consolidate_diagnosis(self.dm.diagnoses, hepa_res + gen_res)
        self.dm.diagnoses = consolidated

        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses(),
            "source": "initial_analysis"
        })

        ranked_questions = await self.merger_agent.process_question("", consolidated, self.qm.get_questions_basic())
        self.qm.add_questions(ranked_questions)

        enriched_q = await self.q_enrich.enrich_questions(self.qm.get_questions_basic())
        self.qm.update_enriched_questions(enriched_q)
        
        await self._push_to_ui({"type": "questions", "questions": self.qm.questions, "source": "initial_analysis"})
        with open('status_update.json', 'w', encoding='utf-8') as f:
            json.dump({
                "is_finished": False,
                "question": self.qm.get_high_rank_question().get("content") if self.qm.get_high_rank_question() else None,
                "education":  ""
            }, f, indent=4)

    async def _push_to_ui(self, payload):
        """Sends JSON updates back to the Frontend via WebSocket."""
        if self.websocket and self.main_loop:
            try:
                asyncio.run_coroutine_threadsafe(self.websocket.send_json(payload), self.main_loop)
            except Exception as e:
                logger.error(f"UI Push Error: {e}")

    async def _check_logic(self, new_text):
        """Main AI Reasoning Branch: Questions, Education, Analytics, and Diagnosis."""
        total_start = time.perf_counter()
        
        # 1. Parallel Tasks Execution
        parallel_start = time.perf_counter()
        
        edu_task = self.education_agent.generate_education(self.transcript_structure, self.em.pool)
        analytics_task = self.analytics_agent.analyze_consultation(self.transcript_structure)
        structure_task = self.transcript_parser.structure_transcription(self.transcript_structure, new_text)
        h_task = self.hepa_agent.get_hepa_diagnosis(new_text, self.patient_info)
        g_task = self.gen_agent.get_gen_diagnosis(new_text, self.patient_info)
        q_check_task = self.qc.check_question(new_text, self.qm.get_unanswered_questions())
        status_task = self.supervisor.check_completion(new_text, self.dm.diagnoses)

        (edu_res, analytics_res, structured_chat, h_res, g_res, answered_qs, status_res) = await asyncio.gather(
            edu_task, analytics_task, structure_task, h_task, g_task, q_check_task, status_task
        )
        
        parallel_duration = time.perf_counter() - parallel_start
        logger.info(f"⏱️ [Parallel Tasks] Completed in {parallel_duration:.2f}s")


        with open('diagnosis_result.json', 'w', encoding='utf-8') as f:
            json.dump({
                "general_diagnosis": g_res,
                "hepato_diagnosis": h_res
            }, f, indent=4)

        # 2. Sequential Processing Start
        processing_start = time.perf_counter()

        # Update Chat
        self.transcript_structure = structured_chat
        await self._push_to_ui({"type": "chat", "data": self.transcript_structure})

        # Update Questions State
        for aq in answered_qs:
            self.qm.update_status(aq['qid'], "asked")
            self.qm.update_answer(aq['qid'], aq['answer'])
        
        # Consolidate Diagnosis
        consolidated = await self.consolidate_agent.consolidate_diagnosis(self.dm.diagnoses, h_res + g_res)
        with open('diagnosis_consolidate.json', 'w', encoding='utf-8') as f:
            json.dump(consolidated, f, indent=4)
        self.dm.diagnoses = consolidated
        
        generated_questions = [i.get('followup_question') for i in h_res] + [i.get('followup_question') for i in g_res]
        self.qm.add_from_strings(generated_questions)

        ranked_questions = await self.ranker.rank_questions(new_text, self.qm.get_questions_basic())
        with open('ranked_questions.json', 'w', encoding='utf-8') as f:
            json.dump(ranked_questions, f, indent=4)
        # Rerank and Enrich Questions
        # ranked_questions = await self.merger_agent.process_question(new_text,  h_res + g_res, self.qm.get_questions_basic())
        print(f"RANKED QUESTIONS: {ranked_questions}")

        self.qm.add_questions(ranked_questions.get('ranked',[]))
        enriched_q = await self.q_enrich.enrich_questions(self.qm.get_questions_basic())
        self.qm.update_enriched_questions(enriched_q)

        # Handle Education
        self.em.add_new_points(edu_res)
        next_ed = self.em.pick_and_mark_asked()

        self.analytics_pool = analytics_res

        # Final UI Push
        await self._push_to_ui({"type": "diagnosis", "diagnosis": self.dm.get_diagnoses()})
        await self._push_to_ui({"type": "questions", "questions": self.qm.questions})
        await self._push_to_ui({"type": "analytics", "data": analytics_res})
        await self._push_to_ui({"type": "status", "data": status_res})
        await self._push_to_ui({"type": "education", "data": self.em.pool})


        with open('master_question.json', 'w', encoding='utf-8') as f:
            json.dump(self.qm.questions, f, indent=4)
        # Update status_update.json
        hr_q = self.qm.get_high_rank_question()
        # self.qm.update_status(hr_q['qid'], "asked")
        

        self.status = status_res.get("end", False)
        if self.status:
            self.running = False
        logger.info(f"🤖 [AI Agent] Consultation status - running: {self.running}, complete: {self.status}")
        if not self.qm.get_high_rank_question():
            self.status = True

        update_object = {
                "is_finished": self.status,
                "question": hr_q.get("content") if hr_q else None,
                "education": next_ed.get("content", "") if next_ed else ""
            }
        logger.info(f"✅ [AI Agent] status_update.json: {update_object}")
        

        with open('status_update.json', 'w', encoding='utf-8') as f:
            json.dump(update_object, f, indent=4)

        processing_duration = time.perf_counter() - processing_start
        total_duration = time.perf_counter() - total_start

        logger.info(f"⏱️ [Processing] UI & Logic updates took {processing_duration:.2f}s")
        logger.info(f"✅ [AI Agent] Logic cycle complete. Total time: {total_duration:.2f}s")

    async def _final_wrap(self):
        logger.info("🛑 [Finalization] Consultation complete. Generating final outputs...")
        check_result = await self.checklist_agent.generate_checklist(
            transcript = self.transcript_structure, 
            diagnosis = self.dm.get_diagnoses(),
            question_list = self.qm.questions,
            analytics = self.analytics_pool,
            education_list = self.em.pool
        )

        await self._push_to_ui({"type": "checklist", "data": check_result})


        report_result = await self.report_agent.generate_report(
            transcript=self.transcript_structure,
            question_list=self.qm.questions,
            diagnosis_list=self.dm.get_diagnoses(),
            education_list=self.em.pool,
            analytics=self.analytics_pool
        )

        await self._push_to_ui({"type": "report", "data": report_result})
        logger.info("🛑 [Finalization] Finished")



    async def _logic_loop(self):
        last_processed_text = ""
        while self.running:
            try:
                # if not os.path.exists(TRANSCRIPT_FILE):
                #     await asyncio.sleep(1)
                #     continue

                # with open(TRANSCRIPT_FILE, "r", encoding="utf-8") as f:
                #     lines = f.readlines()
                lines = self.transcript_memory
                full_text = " ".join(lines).strip()

                # Trigger if the text has grown by a certain amount (e.g., 20 characters)
                # or if a new sentence was finalized
                text_has_grown = len(full_text) > len(last_processed_text) + 20
                sentence_was_finalized = self.last_line_count < len(lines)
                
                logger.info(f"🤖 LOGIC LOOP] transcript lines: {len(full_text)},  last_line_count: {last_processed_text}")

                if text_has_grown:
                    full_text = " ".join([l.strip() for l in lines if l.strip()])
                    logger.info(f"🤖 [AI Agent] Analyzing updated transcript...")
                    
                    await self._check_logic(full_text)
                    
                    self.last_line_count = len(lines)
                    await asyncio.sleep(5) # Cooldown
                else:
                    await asyncio.sleep(1)
                    print("Waiting for new transcript lines...")

                if self.status:
                    await self._final_wrap()
                    self.running = False
                    logger.info(f"✅ [Logic Thread] Consultation marked as complete. Exiting logic loop. status : {self.status}, running: {self.running}")

                    break
                
            except Exception as e:
                logger.error(f"❌ [Logic Thread] Error: {e}")
                traceback.print_exc()
                await asyncio.sleep(2)

        

    def stop(self):
        self.running = False

class TranscriberEngine:
    def __init__(self, patient_id, patient_info, websocket, loop):
        self.websocket = websocket
        self.patient_id = patient_id
        self.patient_info = patient_info
        self.main_loop = loop
        self.running = True
        
        # Audio Config
        self.AUDIO_DELAY_SEC = 0.2
        self.SIMULATION_RATE = 24000
        self.TRANSCRIBER_RATE = 16000
        self.resample_state = None
        self.audio_queue = queue.Queue()       
        self.transcript_memory = []
        self.is_sentence_final = True

        # Initialize Logic Thread
        self.logic_thread = TranscriberLogicThread(
            self.patient_info, 
            diagnosis_manager.DiagnosisManager(), 
            question_manager.QuestionPoolManager([]), 
            self.main_loop, self.websocket, self.transcript_memory, self.running
        )
        self.logic_thread.start()

    def add_audio(self, audio_bytes):
        """Receives raw bytes from server.py WebSocket."""
        try:
            # Resample from 24k (Simulation) to 16k (Google STT)
            converted, self.resample_state = audioop.ratecv(
                audio_bytes, 2, 1, self.SIMULATION_RATE, self.TRANSCRIBER_RATE, self.resample_state
            )
            
            # Tag with release time for synchronization delay
            release_time = time.time() + self.AUDIO_DELAY_SEC
            self.audio_queue.put((release_time, converted))
        except Exception as e:
            logger.error(f"Resampling Error: {e}")

    def stt_loop(self):
        """Google STT Streaming."""
        # Block audio processing until Logic Thread completes initial file review
        logger.info("⏳ [Engine] Waiting for initial analysis...")
        self.logic_thread.ready_event.wait()

        client = speech.SpeechClient()
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.TRANSCRIBER_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_long",
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=True)

        def request_generator():
            while self.running:
                try:
                    item = self.audio_queue.get(timeout=1.0)
                    if item is None: return
                    
                    release_time, chunk = item
                    now = time.time()
                    if now < release_time:
                        time.sleep(release_time - now)
                    
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                except queue.Empty:
                    continue

        logger.info(f"🎙️ [STT Loop] Google Stream started with {self.AUDIO_DELAY_SEC}s delay.")
        retries_count = 0
        while self.running:
            try:
                responses = client.streaming_recognize(streaming_config, request_generator())
                
                for response in responses:
                    if not self.running: break
                    if not response.results: continue
                    
                    result = response.results[0]
                    transcript = result.alternatives[0].transcript

                    if not result.is_final:
                        # --- INTERIM CHUNK HANDLING ---
                        if self.is_sentence_final:
                            # Start a new sentence entry
                            self.transcript_memory.append(transcript)
                            self.is_sentence_final = False
                        else:
                            # Update the existing current sentence
                            if self.transcript_memory:
                                self.transcript_memory[-1] = transcript
                        
                        # (Optional) still print for your own terminal view
                        # print(f"🎙️ [LIVE CHUNK]: {transcript}          ")
                        with open("output/chunk_transcript_data.json", "w", encoding="utf-8") as f:
                            json.dump(self.transcript_memory, f)

                    else:
                        # --- FINAL TRANSCRIPT HANDLING ---
                        if self.is_sentence_final:
                            # This handles rare cases where is_final comes without an interim first
                            self.transcript_memory.append(transcript)
                        else:
                            # Update the final version of the current sentence
                            self.transcript_memory[-1] = transcript
                        
                        self.is_sentence_final = True # Mark as done so the next word starts a new index
                        print(f"\n✅ [FINAL SENTENCE]: {transcript}")
                        with open("output/final_transcript_data.json", "w", encoding="utf-8") as f:
                            json.dump(self.transcript_memory, f)

                        
            except Exception as e:
                if self.running:
                    logger.warning(f"🎙️ [STT Restarting], status {self.running}: {e}, retries: {retries_count}")
                    retries_count += 1
                    if retries_count >= 10:
                        logger.error("❌ [STT] Maximum retries reached. Stopping STT loop.")
                        self.running = False
                        break
                time.sleep(0.1)

    def stop(self):
        self.running = False
        self.logic_thread.stop()
        self.audio_queue.put(None)