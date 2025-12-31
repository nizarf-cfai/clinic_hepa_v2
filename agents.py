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
from dotenv import load_dotenv

load_dotenv()
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
        except Exception as e:
            print(f"Error in get_hepa_diagnosis: {e}") 
            return []

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
        except Exception as e:
            print(f"Error in get_gen_diagnosis: {e}")
            return []

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
        except Exception as e:
            print(f"Error in consolidate_diagnosis: {e}")
            return []


class QuestionCheck(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        self.response_schema = {
            "type": "ARRAY",
            "description": "The prioritized list of questions, ranked from most important (index 0) to least important.",
            "items": {
                "type": "OBJECT",
                "properties": {
                "answer": {
                    "type": "STRING",
                    "description": "Answer of the question."
                },
                "qid": {
                    "type": "STRING",
                    "description": "The ID of the question."
                }
                },
                "required": [
                "answer",
                "qid"
                ]
            }
            }
        
        try:
            with open("system_prompts/question_checker.md", "r", encoding="utf-8") as f: self.system_instruction = f.read()
        except: self.system_instruction = "Return true if new info."

    async def check_question(self, transcript, question_pool):
        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=f"Question Pool:\n{json.dumps(question_pool)}\nTranscript:\n{json.dumps(transcript)}",
                config=types.GenerateContentConfig(response_mime_type="application/json", 
                response_schema=self.response_schema, 
                system_instruction=self.system_instruction, 
                temperature=0.0)
            )
            res = json.loads(response.text)
            return res
        except Exception as e:
            print(f"Error in check_question: {e}")
            return []

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
        except Exception as e:
            print(f"Error in process_question: {e}")
            return []

class InterviewSupervisor(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        # Updated schema to include both completion status and current state
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "end": {
                    "type": "BOOLEAN",
                    "description": "True if the clinical intake is sufficient and the interview should terminate."
                },
                "state": {
                    "type": "STRING",
                    "enum": ["start", "mid", "end"],
                    "description": "The current phase of the consultation."
                }
            },
            "required": ["end", "state"]
        }
        
        try:
            with open("system_prompts/supervisor_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Identify the interview state and determine if it is clinically complete."

    async def check_completion(self, transcript, diagnosis_hypotheses):
        try:
            user_content = (
                f"Hypothesis Diagnosis Data:\n{json.dumps(diagnosis_hypotheses)}\n\n"
                f"Ongoing Interview Transcript:\n{transcript}"
            )

            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=user_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=self.response_schema, 
                    system_instruction=self.system_instruction, 
                    temperature=0.0
                )
            )
            
            return json.loads(response.text) # Returns {"end": bool, "state": "..."}
            
        except Exception as e:
            print(f"Error in InterviewSupervisor: {e}")
            return {"end": False, "state": "mid"}
        


class TranscribeStructureAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        # Schema defined to return a list of {"role": "...", "message": "..."}
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "role": {
                        "type": "STRING",
                        "description": "The identity of the speaker."
                    },
                    "message": {
                        "type": "STRING",
                        "description": "The cleaned transcript text associated with this role."
                    }
                },
                "required": ["role", "message"]
            }
        }
        
        try:
            # Assumes you will create this prompt file
            with open("system_prompts/transcribe_structure_agent.md", "r", encoding="utf-8") as f: 
                self.system_instruction = f.read()
        except Exception: 
            self.system_instruction = (
                "You are an expert transcription parser. Your task is to take raw, unformatted "
                "transcription text and structure it into a clear dialogue format. Identify different "
                "speakers based on context and separate their statements into roles and messages."
            )

    async def structure_transcription(self, existing_transcript: list, new_raw_text: str):
        """
        existing_transcript: List of dicts [{"role": "...", "message": "..."}]
        new_raw_text: String of raw text to be parsed
        """
        try:
            # Constructing the prompt to show the history and the new data
            prompt_content = (
                f"Existing Structured Transcript:\n{json.dumps(existing_transcript)}\n\n"
                f"New Raw Text:\n{new_raw_text}"
            )

            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite", 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=self.response_schema, 
                    system_instruction=self.system_instruction, 
                    temperature=0.0
                )
            )
            
            res = json.loads(response.text)
            return res
            
        except Exception as e:
            print(f"Error in structure_transcription: {e}")
            return existing_transcript # Return current state if it fails


class QuestionEnrichmentAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        # Schema focusing on the metadata of the question card
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "qid": {"type": "STRING"},
                    "headline": {
                        "type": "STRING",
                        "description": "Short, punchy title for the question (e.g., 'Past Surgeries')."
                    },
                    "domain": {
                        "type": "STRING",
                        "description": "Broad clinical category (e.g., History, Medication, Symptom Check)."
                    },
                    "system_affected": {
                        "type": "STRING",
                        "description": "The biological system (e.g., Respiratory, Cardiovascular, None)."
                    },
                    "clinical_intent": {
                        "type": "STRING",
                        "description": "Brief explanation of why this question is clinically relevant."
                    },
                    "tags": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    }
                },
                "required": ["qid", "headline", "domain", "system_affected", "clinical_intent", "tags"]
            }
        }

        try:
            with open("system_prompts/question_enrichment_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except:
            self.system_instruction = "Enrich medical questions with UI and clinical metadata."

    async def enrich_questions(self, questions_list: list):
        if not questions_list:
            return []

        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=f"Questions to process:\n{json.dumps(questions_list)}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.0
                )
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Error in enrichment: {e}")
            return []


class ConsultationAnalyticAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "overall_score": {"type": "NUMBER"},
                "metrics": {
                    "type": "OBJECT",
                    "properties": {
                        "empathy": {
                            "type": "OBJECT",
                            "properties": {
                                "score": {"type": "INTEGER"},
                                "reasoning": {"type": "STRING"},
                                "example_quote": {"type": "STRING"}
                            }
                        },
                        "clarity": {
                            "type": "OBJECT",
                            "properties": {
                                "score": {"type": "INTEGER"},
                                "reasoning": {"type": "STRING"},
                                "feedback": {"type": "STRING"}
                            }
                        },
                        "information_gathering": {
                            "type": "OBJECT",
                            "properties": {
                                "score": {"type": "INTEGER"},
                                "reasoning": {"type": "STRING"}
                            }
                        },
                        "patient_engagement": {
                            "type": "OBJECT",
                            "properties": {
                                "score": {"type": "INTEGER"},
                                "turn_taking_ratio": {"type": "STRING", "description": "e.g., '60% Nurse / 40% Patient'"}
                            }
                        }
                    }
                },
                "key_strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
                "improvement_areas": {"type": "ARRAY", "items": {"type": "STRING"}},
                "sentiment_trend": {"type": "STRING", "description": "How the patient's mood shifted (e.g., 'Anxious to Relieved')"}
            },
            "required": ["overall_score", "metrics", "key_strengths", "improvement_areas"]
        }

        try:
            with open("system_prompts/analytic_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except:
            self.system_instruction = "Analyze the nurse-patient transcript and provide clinical communication scores."

    async def analyze_consultation(self, structured_transcript: list):
        """
        Takes the structured transcript (list of role/message dicts) 
        and returns a deep dive analysis.
        """
        if not structured_transcript:
            return {}

        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=f"Transcript for Analysis:\n{json.dumps(structured_transcript)}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.0
                )
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Error in consultation analysis: {e}")
            return {}



class PatientEducationAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "headline": {
                        "type": "STRING",
                        "description": "Short title (e.g., 'Hydration Tip')."
                    },
                    "content": {
                        "type": "STRING",
                        "description": "The advice or reassurance text."
                    },
                    "category": {
                        "type": "STRING", 
                        "enum": ["Safety", "Medication", "Reassurance", "Next Steps"]
                    },
                    "urgency": {
                        "type": "STRING",
                        "enum": ["Low", "Normal", "High"]
                    },
                    "context_reference": {
                        "type": "STRING",
                        "description": "The specific patient mention this relates to."
                    }
                },
                "required": ["headline", "content", "category", "urgency", "context_reference"]
            }
        }

        try:
            with open("system_prompts/patient_education_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except:
            self.system_instruction = "Generate NEW patient education points. Do not repeat existing ones."

    async def generate_education(self, transcript: list, existing_education: list):
        """
        transcript: The current full dialogue.
        existing_education: List of education points already generated in previous turns.
        """
        if not transcript:
            return []

        try:
            # We provide both the transcript and the already-sent list to prevent duplicates
            user_content = (
                f"ALREADY PROVIDED EDUCATION:\n{json.dumps(existing_education)}\n\n"
                f"CURRENT TRANSCRIPT:\n{json.dumps(transcript)}"
            )

            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=user_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.0
                )
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Error in PatientEducationAgent: {e}")
            return []


class ClinicalChecklistAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        # New Schema matching your requirement
        self.response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {
                        "type": "STRING",
                        "description": "Unique identifier (e.g., '1', '2')."
                    },
                    "title": {
                        "type": "STRING",
                        "description": "Short name of the clinical best practice criteria."
                    },
                    "description": {
                        "type": "STRING",
                        "description": "Detailed evidence (quote) if completed, or explanation of the gap if not."
                    },
                    "category": {
                        "type": "STRING", 
                        "enum": ["communication", "symptoms", "safety", "education"],
                        "description": "The logical grouping of the checkpoint."
                    },
                    "completed": {
                        "type": "BOOLEAN",
                        "description": "True if the criteria was met."
                    },
                    "priority": {
                        "type": "STRING",
                        "enum": ["high", "medium", "low"],
                        "description": "The clinical importance of this specific point."
                    }
                },
                "required": ["id", "title", "description", "category", "completed", "priority"]
            }
        }

        try:
            with open("system_prompts/clinical_checklist_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Generate a clinical checklist with reasoning based on the transcript."

    async def generate_checklist(self, transcript, diagnosis, question_list, analytics, education_list):
        if not transcript: return []
        try:
            user_content = (
                f"CONTEXT DATA:\n"
                f"Preliminary Diagnosis: {diagnosis}\n"
                f"Consultation Analytics: {json.dumps(analytics)}\n"
                f"Questions Suggested: {json.dumps(question_list)}\n"
                f"Patient Education Provided: {json.dumps(education_list)}\n\n"
                f"TRANSCRIPT TO EVALUATE:\n{json.dumps(transcript)}"
            )

            response = await self.client.aio.models.generate_content(
                model="gemini-2.0-flash-lite", # Adjusted to latest naming convention
                contents=user_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.0
                )
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Error in ClinicalChecklistAgent: {e}")
            return []
        
class ComprehensiveReportAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()
        
        # 1. Load System Prompt from file
        try:
            with open("system_prompts/comprehensive_report_agent.md", "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except FileNotFoundError:
            self.system_instruction = "Synthesize the provided clinical data and transcript into a structured medical report."

        # 2. Define Response Schema
        self.response_schema = {
            "type": "OBJECT",
            "properties": {
                "clinical_handover": {
                    "type": "OBJECT",
                    "properties": {
                        "hpi_narrative": {
                            "type": "STRING",
                            "description": "A professional 4-6 sentence History of Present Illness summary based on transcript and logs."
                        },
                        "key_biomarkers_extracted": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "List of lab values or specific signs extracted (e.g. 'AST 450', 'Temp 39C')."
                        },
                        "clinical_impression_summary": {
                            "type": "STRING",
                            "description": "A brief summary of the primary suspected diagnosis and severity."
                        },
                        "suggested_doctor_actions": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "Specific questions or exams the doctor should perform next."
                        }
                    },
                    "required": ["hpi_narrative", "key_biomarkers_extracted", "clinical_impression_summary"]
                },
                "audit_summary": {
                    "type": "OBJECT",
                    "properties": {
                        "performance_narrative": {
                            "type": "STRING",
                            "description": "A qualitative summary of the nurse's soft skills and communication style."
                        },
                        "areas_for_improvement_summary": {
                            "type": "STRING",
                            "description": "Consolidated advice for the nurse."
                        }
                    }
                }
            },
            "required": ["clinical_handover", "audit_summary"]
        }

    async def generate_report(self, 
                              transcript: list,
                              question_list: list, 
                              diagnosis_list: list, 
                              education_list: list, 
                              analytics: dict):
        """
        Dumps raw arguments (including transcript) into the prompt and returns a structured AI report.
        """
        
        # NO FILTERING: Just dumping the raw data strings into the prompt context
        user_content = (
            f"--- RAW DATA START ---\n"
            f"1. RAW_TRANSCRIPT:\n{json.dumps(transcript)}\n\n"
            f"2. QUESTION_LIST_LOGS:\n{json.dumps(question_list)}\n\n"
            f"3. PRELIMINARY_DIAGNOSIS_LOGS:\n{json.dumps(diagnosis_list)}\n\n"
            f"4. PATIENT_EDUCATION_LOGS:\n{json.dumps(education_list)}\n\n"
            f"5. ANALYTICS_METRICS:\n{json.dumps(analytics)}\n"
            f"--- RAW DATA END ---\n\n"
            f"Please generate the Clinical Handover Report based on this data."
        )

        try:
            response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=user_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=self.response_schema,
                    system_instruction=self.system_instruction,
                    temperature=0.0
                )
            )
            return json.loads(response.text)
            
        except Exception as e:
            print(f"Error in ComprehensiveReportAgent: {e}")
            return {"error": "Failed to generate report"}
        

        