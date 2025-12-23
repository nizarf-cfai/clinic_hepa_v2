import asyncio
import threading
import json
import audioop
import queue
import logging
import os
import time
import sys
from google.cloud import speech
from datetime import datetime
# Local Imports
import agents
import diagnosis_manager
import question_manager

logger = logging.getLogger("medforce-backend")

TRANSCRIPT_FILE = "simulation_transcript.txt"

class TranscriberLogicThread(threading.Thread):
    def __init__(self, patient_info, dm, qm, main_loop, websocket):
        super().__init__()
        self.patient_info = patient_info
        self.dm = dm
        self.qm = qm
        self.main_loop = main_loop 
        self.websocket = websocket
        self.running = True
        self.daemon = True 
        self.qc = agents.QuestionCheck()
        # Tracks how many lines we have already processed
        self.last_line_count = 0 

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Initialize Agents
        self.hepa_agent = agents.DiagnosisHepato()
        self.gen_agent = agents.DiagnosisGeneral()
        self.consolidate_agent = agents.DiagnosisConsolidate()
        self.merger_agent = agents.QuestionMerger()
        self.supervisor = agents.InterviewSupervisor()

        # Clear transcript file at start of session
        with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
            f.write("")

        print(f"🩺 [Logic Thread] Monitoring {TRANSCRIPT_FILE}...")

        loop.run_until_complete(self.start_logic())

    async def start_logic(self):
        """Wrapper to ensure initial analysis runs before the logic loop."""
        await self.run_initial_analysis()
        await self._logic_loop()

    async def run_initial_analysis(self):
        print(f"🩺 [Logic Thread] Initial Analysis Starting...")

        initial_instruction = "Initial file review. Please analyze the patient's medical history and profile."
        
        h_coro = self.hepa_agent.get_hepa_diagnosis(initial_instruction, self.patient_info)
        g_coro = self.gen_agent.get_gen_diagnosis(initial_instruction, self.patient_info)
        
        hepa_res, gen_res = await asyncio.gather(h_coro, g_coro)

        # print("HEPA RES:", hepa_res)
        # print("GEN RES:", gen_res)
        # 2. Consolidate
        consolidated = await self.consolidate_agent.consolidate_diagnosis(
            self.dm.diagnoses, hepa_res + gen_res
        )
        self.dm.diagnoses = consolidated

        # 3. Check Status & Questions
        ranked_questions = await self.merger_agent.process_question(
            "", consolidated, self.qm.get_questions_basic()
        )
        self.qm.add_questions(ranked_questions)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses()
        })
        # with open(f'output/init_diagnosis_{timestamp}.json', 'w', encoding='utf-8') as f:
        #     json.dump(self.dm.get_diagnoses(), f, indent=4)

        await self._push_to_ui({
            "type": "questions",
            "questions": self.qm.questions,
            "source" : "initial_analysis"
        })
        # with open(f'output/init_question_{timestamp}.json', 'w', encoding='utf-8') as f:
        #     json.dump(self.qm.questions, f, indent=4)
        print(f"🩺 [Logic Thread] Initial Analysis Finished.")



    async def _push_to_ui(self, payload):
        if self.websocket and self.main_loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.websocket.send_json(payload),
                    self.main_loop
                )
            except Exception:
                pass

    async def _check_q(self, transcript, question_pool):
        print(f"🩺 [Logic Thread] Checking Questions...")
        answered_list = await self.qc.check_question(transcript, question_pool)
        for aq in answered_list:
            print(f"✅ [Question Check] QID {aq['qid']} answered with: {aq['answer']}")
            self.qm.update_status(aq['qid'], "asked")
            self.qm.update_answer(aq['qid'], aq['answer'])

        
        status = await self.supervisor.check_completion(transcript, self.dm.diagnoses)
        next_q_obj = self.qm.get_high_rank_question()
        next_q_text = next_q_obj.get("content") if next_q_obj else None

        with open('status_update.json', 'w', encoding='utf-8') as f:
            json.dump({
                "is_finished": status.get("end", False),
                "question": next_q_text
            }, f, indent=4)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        await self._push_to_ui({
            "type": "diagnosis",
            "diagnosis": self.dm.get_diagnoses()
        })
        # with open(f'output/diagnosis_{timestamp}.json', 'w', encoding='utf-8') as f:
        #     json.dump(self.dm.get_diagnoses(), f, indent=4)

        await self._push_to_ui({
            "type": "questions",
            "questions": self.qm.questions,
            "source" : "logic_check"
        })
        # with open(f'output/question_{timestamp}.json', 'w', encoding='utf-8') as f:
        #     json.dump(self.qm.questions, f, indent=4)

        print(f"🩺 [Logic Thread] Checking Questions Finished.")
        

    async def _logic_loop(self):
        
        while self.running:
            try:
                self.qm.update_pool()
                if not os.path.exists(TRANSCRIPT_FILE):
                    await asyncio.sleep(1)
                    continue

                # Read the file
                with open(TRANSCRIPT_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                # If there are new lines, process them
                if len(lines) > self.last_line_count:
                    # Capture only the new lines
                    new_lines = lines[self.last_line_count:]
                    new_transcript_text = " ".join([l.strip() for l in lines if l.strip()])
                    
                    await self._check_q(new_transcript_text, self.qm.get_questions_basic())

                    self.last_line_count = len(lines)

                    if not new_transcript_text:
                        continue

                    print(f"\n🤖 [AI Agent] File Update Detected. Analyzing: {new_transcript_text[:70]}...")

                    # 1. Run Diagnoses
                    h_task = self.hepa_agent.get_hepa_diagnosis(new_transcript_text, self.patient_info)
                    g_task = self.gen_agent.get_gen_diagnosis(new_transcript_text, self.patient_info)
                    hepa_res, gen_res = await asyncio.gather(h_task, g_task)

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


                    # 4. Push to UI & Save status_update.json
                    

                    await self._push_to_ui({
                        "type": "ai_update",
                        "consolidated": consolidated,
                        "ranked_questions": self.qm.get_questions()
                    })


                    print(f"✅ [AI Agent] Analysis Cycle Complete.")
                    
                    # Pause as requested to allow simulation to catch up
                    await asyncio.sleep(5) 

                else:
                    # No new lines, wait a bit
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"❌ [Logic Thread] Error: {e}")
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
        
        self.SIMULATION_RATE = 24000
        self.TRANSCRIBER_RATE = 16000
        self.resample_state = None

        self.audio_queue = queue.Queue()       

        self.logic_thread = TranscriberLogicThread(
            self.patient_info, 
            diagnosis_manager.DiagnosisManager(), 
            question_manager.QuestionPoolManager([]), 
            self.main_loop, self.websocket
        )
        self.logic_thread.start()

    def add_audio(self, audio_bytes):
        try:
            converted, self.resample_state = audioop.ratecv(
                audio_bytes, 2, 1, self.SIMULATION_RATE, self.TRANSCRIBER_RATE, self.resample_state
            )
            self.audio_queue.put(converted)
        except Exception as e:
            logger.error(f"Resampling Error: {e}")

    def stt_loop(self):
        """Google STT Loop that writes finalized text to a TXT file."""
        client = speech.SpeechClient()
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.TRANSCRIBER_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_long",
        )
        # Enable interim results for live visual feedback in terminal
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=True)

        def request_generator():
            while self.running:
                try:
                    chunk = self.audio_queue.get(timeout=1.0)
                    if chunk is None: return
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                except queue.Empty:
                    continue

        print(f"🎙️ [STT Loop] Background thread active.")

        while self.running:
            try:
                responses = client.streaming_recognize(streaming_config, request_generator())
                
                for response in responses:
                    if not self.running: break
                    if not response.results: continue
                    
                    result = response.results[0]
                    transcript = result.alternatives[0].transcript

                    if not result.is_final:
                        # Live print for visual debug
                        pass
                        # sys.stdout.write(f"\r🎙️ [STT Live]: {transcript}...")
                        # sys.stdout.flush()
                    else:
                        print(f"\n🎙️ [STT FINAL]: {transcript}")
                        
                        # --- DUMP TO FILE ---
                        with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
                            f.write(transcript + "\n")
                            f.flush()
                            os.fsync(f.fileno()) # Force write to disk
                        
            except Exception as e:
                if "400" in str(e) or "Timeout" in str(e):
                    if self.running:
                        pass # Quietly reconnect on silence
                else:
                    print(f"\n🎙️ [STT Error]: {e}")
                time.sleep(0.1)

    def stop(self):
        self.running = False
        self.logic_thread.stop()
        self.audio_queue.put(None)