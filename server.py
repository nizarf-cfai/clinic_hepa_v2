# --- server.py ---
import os
import json
import logging
import traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from pydantic import BaseModel
from google.cloud import storage
from dotenv import load_dotenv
import asyncio
import threading
from transcriber_engine import TranscriberEngine
from utils import fetch_gcs_text_internal # Assuming this helper exists
# --- Local Modules ---
from simulation import SimulationManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-backend")

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models ---

class PatientFileRequest(BaseModel):
    pid: str       # e.g., "p001"
    file_name: str # e.g., "lab_results.png" or "history.md"

class AdminFileSaveRequest(BaseModel):
    pid: str
    file_name: str
    content: str

# This was missing in your code!
class AdminPatientRequest(BaseModel):
    pid: str

# --- Endpoints ---

@app.websocket("/ws/transcriber")
async def websocket_transcriber_endpoint(websocket: WebSocket):
    """
    Main entry point for the AI Transcriber.
    - Receives configuration (JSON) to start.
    - Receives raw audio (Bytes) to process.
    - Pushes AI updates (JSON) back to the frontend.
    """
    with open("questions.json", "r") as file:
        # Load the contents into a Python dictionary or list
        questions = json.load(file)
    with open("question_pool.json", "w") as file:
        # 'indent=4' makes the file human-readable
        json.dump(questions, file, indent=4)

    await websocket.accept()
    
    # We capture the main event loop to allow background threads 
    # to send JSON updates back to this specific websocket.
    main_loop = asyncio.get_running_loop()
    engine = None

    logger.info("🔌 Frontend connected to /ws/transcriber")

    try:
        while True:
            # Wait for any message (JSON or Binary)
            message = await websocket.receive()

            # --- 1. HANDLE JSON COMMANDS ---
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    
                    if data.get("type") == "start":
                        patient_id = data.get("patient_id", "P0001")
                        logger.info(f"🚀 Starting Transcriber Engine for {patient_id}")
                        
                        # Fetch patient context from GCS
                        patient_info = fetch_gcs_text_internal(patient_id, "patient_info.md")
                        
                        # Initialize the Engine
                        # Note: This automatically starts the TranscriberLogicThread internally
                        engine = TranscriberEngine(
                            patient_id=patient_id,
                            patient_info=patient_info,
                            websocket=websocket,
                            loop=main_loop
                        )
                        
                        # Start the Google STT stream in its own background thread
                        stt_thread = threading.Thread(
                            target=engine.stt_loop, 
                            daemon=True,
                            name=f"STT_{patient_id}"
                        )
                        stt_thread.start()
                        
                        await websocket.send_json({
                            "type": "system", 
                            "message": f"Transcriber initialized for {patient_id}"
                        })

                except json.JSONDecodeError:
                    logger.error("Received invalid JSON from frontend")

            # --- 2. HANDLE BINARY AUDIO DATA ---
            elif "bytes" in message:
                if engine and engine.running:
                    # Pass the raw audio bytes into the engine's audio queue
                    engine.add_audio(message["bytes"])
                else:
                    # Optional: Log if audio is arriving before 'start' command
                    pass

    except WebSocketDisconnect:
        logger.info("👋 Frontend disconnected from /ws/transcriber")
    except Exception as e:
        logger.error(f"❌ Transcriber WebSocket Error: {e}")
        traceback.print_exc()
    finally:
        # --- 3. CLEANUP ---
        if engine:
            logger.info("🧹 Stopping Transcriber Engine...")
            engine.stop()


@app.websocket("/ws/simulation")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    manager = None 
    try:
        data = await websocket.receive_json()

        if isinstance(data, dict) and data.get("type") == "start":
            patient_id = data.get("patient_id", "P0001")
            gender = data.get("gender", "Male")
            
            manager = SimulationManager(websocket, patient_id, gender)
            await manager.run()
            
    except WebSocketDisconnect:
        logger.info("Client disconnected")
        if manager:
            manager.running = False
            if hasattr(manager, 'logic_thread'): 
                manager.logic_thread.stop()
    except Exception as e:
        traceback.print_exc()
        logger.error(f"WebSocket Error: {e}")
        if manager:
            manager.running = False

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_ui():
    """Serves the Admin UI HTML file."""
    try:
        # Ensure admin_ui.html is in the same directory as server.py
        with open("admin_ui.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: admin_ui.html not found on server.</h1>", status_code=404)


@app.post("/api/get-patient-file")
def get_patient_file(request: PatientFileRequest):
    """
    Retrieves a file from gs://clinic_sim/patient_profile/{pid}/{file_name}
    """
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/{request.file_name}"
    
    logger.info(f"📥 Fetching GCS: gs://{BUCKET_NAME}/{blob_path}")

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)

        if not blob.exists():
            logger.warning(f"File not found: {blob_path}")
            return JSONResponse(
                status_code=404, 
                content={"error": "File not found", "path": blob_path}
            )

        file_ext = request.file_name.lower().split('.')[-1]

        if file_ext == 'json':
            content = blob.download_as_text()
            return JSONResponse(content=json.loads(content))
        elif file_ext in ['md', 'txt']:
            content = blob.download_as_text()
            return Response(content=content, media_type="text/markdown")
        elif file_ext in ['png', 'jpg', 'jpeg']:
            content = blob.download_as_bytes()
            media_type = "image/png" if file_ext == 'png' else "image/jpeg"
            return Response(content=content, media_type=media_type)
        else:
            content = blob.download_as_bytes()
            return Response(content=content, media_type="application/octet-stream")

    except Exception as e:
        logger.error(f"GCS API Error: {e}")
        return JSONResponse(
            status_code=500, 
            content={"error": str(e)}
        )


# ==========================================
# ADMIN ENDPOINTS
# ==========================================

@app.get("/api/admin/list-files/{pid}")
def list_patient_files(pid: str):
    """Lists all files in GCS for a specific patient ID."""
    BUCKET_NAME = "clinic_sim"
    prefix = f"patient_profile/{pid}/"
    
    try:
        storage_client = storage.Client()
        blobs = storage_client.list_blobs(BUCKET_NAME, prefix=prefix)
        
        file_list = []
        for blob in blobs:
            # Remove the prefix from the name for cleaner UI
            clean_name = blob.name.replace(prefix, "")
            if clean_name: # Avoid listing the directory itself
                file_list.append({
                    "name": clean_name,
                    "full_path": blob.name,
                    "size": blob.size,
                    "updated": blob.updated.isoformat() if blob.updated else None
                })
        
        return JSONResponse(content={"files": file_list})
    except Exception as e:
        logger.error(f"List Files Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/save-file")
def save_patient_file(request: AdminFileSaveRequest):
    """Creates or Updates a text-based file."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{request.pid}/{request.file_name}"
    
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        
        # Upload content (Text/Markdown/JSON)
        blob.upload_from_string(request.content, content_type="text/plain")
        
        logger.info(f"💾 Saved file: {blob_path}")
        return JSONResponse(content={"message": "File saved successfully", "path": blob_path})
    except Exception as e:
        logger.error(f"Save File Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/admin/delete-file")
def delete_patient_file(pid: str, file_name: str):
    """Deletes a file."""
    BUCKET_NAME = "clinic_sim"
    blob_path = f"patient_profile/{pid}/{file_name}"
    
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        
        if blob.exists():
            blob.delete()
            logger.info(f"🗑️ Deleted file: {blob_path}")
            return JSONResponse(content={"message": "File deleted successfully"})
        else:
            return JSONResponse(status_code=404, content={"error": "File not found"})
            
    except Exception as e:
        logger.error(f"Delete File Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/list-patients")
def list_patients():
    """Lists all 'folders' (prefixes) under patient_profile/"""
    BUCKET_NAME = "clinic_sim"
    prefix = "patient_profile/"
    
    try:
        storage_client = storage.Client()
        # Using delimiter='/' mimics directory listing
        blobs = storage_client.list_blobs(BUCKET_NAME, prefix=prefix, delimiter="/")
        
        # We must iterate over the iterator to populate .prefixes
        list(blobs) 
        
        patients = []
        for p in blobs.prefixes:
            # p comes back as "patient_profile/p001/" -> we want "p001"
            parts = p.rstrip('/').split('/')
            if parts:
                patients.append(parts[-1])
                
        return JSONResponse(content={"patients": patients})
    except Exception as e:
        logger.error(f"List Patients Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/create-patient")
def create_patient(request: AdminPatientRequest):
    """Creates a new patient folder by creating an initial empty file."""
    BUCKET_NAME = "clinic_sim"
    # GCS folders don't exist without files. We create a default info file.
    blob_path = f"patient_profile/{request.pid}/patient_info.md"
    
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        
        if blob.exists():
             return JSONResponse(status_code=400, content={"error": "Patient already exists"})

        blob.upload_from_string("# Patient Profile\nName: \nAge: ", content_type="text/markdown")
        
        return JSONResponse(content={"message": "Patient created", "pid": request.pid})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/admin/delete-patient")
def delete_patient(pid: str):
    """Deletes a patient folder and ALL files inside it."""
    BUCKET_NAME = "clinic_sim"
    prefix = f"patient_profile/{pid}/"
    
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        if not blobs:
            return JSONResponse(status_code=404, content={"error": "Patient not found"})

        bucket.delete_blobs(blobs)
        logger.info(f"🗑️ Deleted patient folder: {prefix}")
        return JSONResponse(content={"message": f"Deleted {len(blobs)} files for patient {pid}"})
            
    except Exception as e:
        logger.error(f"Delete Patient Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})