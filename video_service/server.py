"""
Harikatha Video Service — a small, separate web service (port 8001) that turns a performance into an MP4.

It does not import or depend on the main backend. The contract is just:

  POST /api/video/jobs          {"performance": {...}, "audio_url": "...", "images": "placeholder"|"ai", ...}
                                -> {"job_id": "..."}
  GET  /api/video/jobs/{id}     -> {"status": "queued|running|done|failed", "progress": 0..1, "message", "video_url"}
  GET  /api/video/jobs/{id}/file -> the MP4

Videos take minutes, so the POST returns immediately and the frontend polls the GET.

Run:  python -m uvicorn server:app --reload --port 8001   (from inside video_service/)
"""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import video_core

HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
UPLOAD_DIR = HERE / "output" / "uploads"
OUTPUT_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

# Settings come from environment variables so nothing is hard-coded.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
SD_MODEL = os.getenv("SD_MODEL", "stabilityai/sd-turbo")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
# Only download narration from these hosts (the main backend), never from arbitrary URLs.
AUDIO_HOSTS = set(os.getenv("AUDIO_HOSTS", "localhost,127.0.0.1").split(","))

app = FastAPI(title="Harikatha Video Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- jobs
class VideoRequest(BaseModel):
    performance: dict[str, Any] = Field(description="The performance ({title, sections}) or a full saved record")
    audio_url: str | None = Field(default=None, description="Optional narration WAV URL from the main backend")
    images: Literal["placeholder", "ai"] = "placeholder"
    ai_prompts: bool = Field(default=False, description="Let the local LLM write a picture prompt per section")
    subtitles: bool = True


JOBS: dict[str, dict[str, Any]] = {}
WORK_QUEUE: "queue.Queue[tuple[str, dict, Path | None, VideoRequest]]" = queue.Queue()


def worker() -> None:
    """One video at a time (the GPU can only do one well). Runs forever in a background thread."""
    while True:
        job_id, data, audio, req = WORK_QUEUE.get()
        job = JOBS[job_id]
        job["status"] = "running"

        def progress(fraction: float, message: str) -> None:
            job["progress"], job["message"] = round(fraction, 3), message

        try:
            video_core.render_video(
                data, OUTPUT_DIR / f"{job_id}.mp4", audio, images=req.images, ai_prompts=req.ai_prompts,
                subtitles=req.subtitles, sd_model=SD_MODEL, ollama_url=OLLAMA_URL, ollama_model=OLLAMA_MODEL,
                progress=progress)
            job.update(status="done", progress=1.0, video_url=f"/api/video/jobs/{job_id}/file")
        except Exception as exc:  # show the reason in the UI instead of crashing the worker
            job.update(status="failed", message=str(exc))
        finally:
            WORK_QUEUE.task_done()


threading.Thread(target=worker, daemon=True).start()


def fetch_audio(url: str, job_id: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in AUDIO_HOSTS:
        raise HTTPException(400, f"audio_url must point to one of: {', '.join(sorted(AUDIO_HOSTS))}")
    try:
        r = httpx.get(url, timeout=60)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(400, f"Could not download narration: {exc}") from exc
    path = UPLOAD_DIR / f"{job_id}.wav"
    path.write_bytes(r.content)
    return path


def enqueue(data: dict, audio: Path | None, req: VideoRequest, job_id: str) -> dict:
    try:
        video_core.get_sections(data)  # fail fast on bad input, before queueing
    except video_core.VideoError as exc:
        raise HTTPException(400, str(exc)) from exc
    if req.images == "ai" and not video_core.ai_available():
        raise HTTPException(400, "AI images are not installed. Run: pip install -r requirements-ai.txt")
    JOBS[job_id] = {"job_id": job_id, "status": "queued", "progress": 0.0, "message": "Waiting", "video_url": None}
    WORK_QUEUE.put((job_id, data, audio, req))
    return {"job_id": job_id}


# ---------------------------------------------------------------- endpoints
@app.get("/api/video/health")
def health() -> dict[str, Any]:
    import shutil
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg")), "ai_images": video_core.ai_available(),
            "queued": WORK_QUEUE.qsize()}


@app.post("/api/video/jobs")
def create_job(req: VideoRequest) -> dict:
    """Main entry point for the app: send the performance JSON the frontend already has."""
    job_id = uuid.uuid4().hex[:12]
    audio = fetch_audio(req.audio_url, job_id) if req.audio_url else None
    return enqueue(req.performance, audio, req, job_id)


@app.post("/api/video/jobs/upload")
async def create_job_upload(performance: UploadFile = File(...), audio: UploadFile | None = File(None),
                            images: Literal["placeholder", "ai"] = Form("placeholder"),
                            ai_prompts: bool = Form(False), subtitles: bool = Form(True)) -> dict:
    """Same thing with file uploads — used by the built-in test page."""
    job_id = uuid.uuid4().hex[:12]
    try:
        data = json.loads(await performance.read())
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "That file is not valid JSON.") from exc
    audio_path = None
    if audio is not None and audio.filename:
        audio_path = UPLOAD_DIR / f"{job_id}{Path(audio.filename).suffix or '.wav'}"
        audio_path.write_bytes(await audio.read())
    req = VideoRequest(performance=data, images=images, ai_prompts=ai_prompts, subtitles=subtitles)
    return enqueue(data, audio_path, req, job_id)


@app.get("/api/video/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found")
    return JOBS[job_id]


@app.get("/api/video/jobs/{job_id}/file")
def get_file(job_id: str):
    path = OUTPUT_DIR / f"{job_id}.mp4"
    if JOBS.get(job_id, {}).get("status") != "done" or not path.exists():
        raise HTTPException(404, "Video not ready")
    return FileResponse(path, media_type="video/mp4", filename=f"harikatha-{job_id}.mp4")


@app.get("/", response_class=HTMLResponse)
def test_page() -> str:
    """A tiny standalone page so the service can be demoed even if the main frontend is mid-change."""
    return (HERE / "test_page.html").read_text(encoding="utf-8")
