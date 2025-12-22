# --- agents.py ---
import os
import json
import base64
import uuid
import asyncio
import logging
from google import genai
from google.genai import types
from fastapi import WebSocket

# Configure logging
logger = logging.getLogger("medforce-backend")

# --- Configuration ---
VOICE_MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"
ADVISOR_MODEL = "gemini-2.5-flash" 
DIAGNOSER_MODEL = "gemini-2.5-flash-lite" 
RANKER_MODEL = "gemini-2.5-flash-lite" 

class BaseLogicAgent:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=os.getenv("PROJECT_ID"), location=os.getenv("PROJECT_LOCATION", "us-central1"))


class TextBridgeAgent:
    def __init__(self, name, system_instruction, voice_name):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        self.client = genai.Client(
            vertexai=True, 
            project=os.getenv("PROJECT_ID"), 
            location=os.getenv("PROJECT_LOCATION", "us-central1")
        )
        self.session = None

    def get_connection_context(self):
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"], 
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        return self.client.aio.live.connect(model=VOICE_MODEL, config=config)

    def set_session(self, session):
        self.session = session

    async def speak_and_stream(self, text_input, websocket: WebSocket, highlighter=None, diagnosis_context=None):
        if not self.session: return None, []
        
        try:
            await self.session.send(input=text_input, end_of_turn=True)
        except Exception:
            return None, []

        turn_id = str(uuid.uuid4())
        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                if data := response.data:
                    b64_audio = base64.b64encode(data).decode('utf-8')
                    await websocket.send_json({
                        "type": "audio",
                        "id": turn_id,
                        "speaker": self.name,
                        "data": b64_audio
                    })
                    await asyncio.sleep(0.005) 

                if response.server_content and response.server_content.output_transcription:
                    if text_chunk := response.server_content.output_transcription.text:
                        text_accumulator.append(text_chunk)
                        await websocket.send_json({
                            "type": "text_delta",
                            "id": turn_id,
                            "speaker": self.name,
                            "text": text_chunk,
                        })

                if response.server_content and response.server_content.turn_complete:
                    await websocket.send_json({
                        "type": "turn_complete",
                        "id": turn_id,
                        "speaker": self.name
                    })
                    
                    full_text = "".join(text_accumulator).strip()
                    if full_text:
                        highlights = []
                        if highlighter and diagnosis_context:
                            try:
                                highlights = await highlighter.highlight_text(full_text, diagnosis_context)
                            except: pass

                        await websocket.send_json({
                            "type": "transcript",
                            "id": turn_id,
                            "speaker": self.name,
                            "text": full_text,
                            "highlights": highlights
                        })
                        return full_text, highlights
                    return "[...]", []
                    
            return None, []
        except Exception as e:
            logger.error(f"Stream Error ({self.name}): {e}")
            return None, []

class DiagnosisHepato(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                "did": {
                    "type": "STRING",
                    "description": "A random 5-character alphanumeric ID."
                },
                "diagnosis": {
                    "type": "STRING",
                    "description": "The specific diagnosis using the syntax: [Pathology] + [Trigger/Cause] + [Acuity/Stage]."
                },
                "indicators_point": {
                    "type": "ARRAY",
                    "items": {
                    "type": "STRING"
                    },
                    "description": "List of specific symptoms, history, or patient quotes supporting this diagnosis."
                },
                "reasoning": {
                    "type": "STRING",
                    "description": "Clinical deduction explaining why the indicators lead to this diagnosis."
                },
                "followup_question": {
                    "type": "STRING",
                    "description": "A targeted question to ask the patient to confirm the diagnosis or rule out differentials."
                }
                },
                "required": [
                "did",
                "diagnosis",
                "indicators_point",
                "reasoning",
                "followup_question"
                ]
            }
            }
        
        try:
            with open("system_prompts/hepato_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def get_hepa_diagnosis(self, conversation_history, patient_info):
        if not conversation_history: return False, "Empty"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=f"Patient Info:\n{patient_info}\n\nTranscript:\n{json.dumps(conversation_history)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", 
                response_schema=self.response_schema, 
                system_instruction=self.system_instruction, 
                temperature=0.0)
            )
            res = json.loads(response.text)
            return res
        except: return []

class DiagnosisGeneral(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                "did": {
                    "type": "STRING",
                    "description": "A random 5-character alphanumeric ID."
                },
                "diagnosis": {
                    "type": "STRING",
                    "description": "The specific diagnosis using the syntax: [Pathology] + [Trigger/Cause] + [Acuity/Stage]."
                },
                "indicators_point": {
                    "type": "ARRAY",
                    "items": {
                    "type": "STRING"
                    },
                    "description": "List of specific symptoms, history, or patient quotes supporting this diagnosis."
                },
                "reasoning": {
                    "type": "STRING",
                    "description": "Clinical deduction explaining why the indicators lead to this diagnosis."
                },
                "followup_question": {
                    "type": "STRING",
                    "description": "A targeted question to ask the patient to confirm the diagnosis or rule out differentials."
                }
                },
                "required": [
                "did",
                "diagnosis",
                "indicators_point",
                "reasoning",
                "followup_question"
                ]
            }
            }
        
        try:
            with open("system_prompts/general_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def get_gen_diagnosis(self, conversation_history, patient_info):
        if not conversation_history: return False, "Empty"
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=f"Patient Info:\n{patient_info}\n\nHistory:\n{json.dumps(conversation_history)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", 
                response_schema=self.response_schema, 
                system_instruction=self.system_instruction, 
                temperature=0.0)
            )
            res = json.loads(response.text)
            return res
        except: return []

class DiagnosisConsolidate(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                "did": {
                    "type": "STRING",
                    "description": "A random 5-character alphanumeric ID."
                },
                "diagnosis": {
                    "type": "STRING",
                    "description": "The specific diagnosis using the syntax: [Pathology] + [Trigger/Cause] + [Acuity/Stage]."
                },
                "indicators_point": {
                    "type": "ARRAY",
                    "items": {
                    "type": "STRING"
                    },
                    "description": "List of specific symptoms, history, or patient quotes supporting this diagnosis."
                },
                "reasoning": {
                    "type": "STRING",
                    "description": "Clinical deduction explaining why the indicators lead to this diagnosis."
                },
                "followup_question": {
                    "type": "STRING",
                    "description": "A targeted question to ask the patient to confirm the diagnosis or rule out differentials."
                }
                },
                "required": [
                "did",
                "diagnosis",
                "indicators_point",
                "reasoning",
                "followup_question"
                ]
            }
            }
        
        try:
            with open("system_prompts/consolidated_agent.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def consolidate_diagnosis(self, diagnosis_pool, new_diagnosis_list):
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=f"Diagnosis Pool:\n{json.dumps(diagnosis_pool)}\nNew Diagnosis List:\n{json.dumps(new_diagnosis_list)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", 
                response_schema=self.response_schema, 
                system_instruction=self.system_instruction, 
                temperature=0.0)
            )
            res = json.loads(response.text)
            return res
        except: return []


class QuestionMerger(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {
            "type": "ARRAY",
            "description": "The prioritized list of questions, ranked from most important (index 0) to least important.",
            "items": {
                "type": "OBJECT",
                "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The question text."
                },
                "qid": {
                    "type": "STRING",
                    "description": "The ID of the question."
                }
                },
                "required": [
                "question",
                "qid"
                ]
            }
            }
        
        try:
            with open("system_prompts/question_merger.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def process_question(self, transcript, diagnosis_pool, question_pool):
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=f"Diagnosis Pool:\n{json.dumps(diagnosis_pool)}\nQuestion Pool:\n{json.dumps(question_pool)}\nTranscript:\n{json.dumps(transcript)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", 
                response_schema=self.response_schema, 
                system_instruction=self.system_instruction, 
                temperature=0.0)
            )
            res = json.loads(response.text)
            return res
        except: return []

class InterviewSupervisor(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        # Define the strict boolean schema
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "end": {
                    "type": "BOOLEAN",
                    "description": "True if the interview is complete, False if more questions are needed."
                }
            },
            "required": ["end"]
        }
        
        # Load the system instruction
        try:
            with open("system_prompts/supervisor_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Determine if the medical interview is complete based on the transcript and diagnosis hypotheses. Return true to end, false to continue."

    async def check_completion(self, transcript, diagnosis_hypotheses):
        """
        Evaluates if the interview should end.
        :param transcript: List or String of the chat history.
        :param diagnosis_hypotheses: Data regarding potential conditions.
        :return: dict {"end": bool}
        """
        try:
            # Prepare the user content
            user_content = (
                f"Hypothesis Diagnosis Data:\n{json.dumps(diagnosis_hypotheses)}\n\n"
                f"Ongoing Interview Transcript:\n{json.dumps(transcript)}"
            )

            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", # or your preferred version
                contents=user_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=self.response_schema, 
                    system_instruction=self.system_instruction, 
                    temperature=0.0
                )
            )
            
            res = json.loads(response.text)
            return res # Returns {"end": True/False}
            
        except Exception as e:
            print(f"Error in InterviewSupervisor: {e}")
            # Safety default: keep the interview going if the agent fails
            return {"end": False}