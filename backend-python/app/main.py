import logging
import re
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, pipeline

log = logging.getLogger("clipper")
logging.basicConfig(level=logging.INFO)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1)  # one video at a time = safe for small servers
_info_hits: dict[str, deque] = defaultdict(deque)


def cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - config.JOB_TTL_MINUTES * 60
        with LOCK:
            old = [j for j in JOBS.values() if j["created"] < cutoff and j["status"] in ("done", "error", "cancelled")]
            for j in old:
                JOBS.pop(j["id"], None)
        for j in old:
            shutil.rmtree(j["dir"], ignore_errors=True)
            log.info("cleaned job %s", j["id"])


@asynccontextmanager
async def lifespan(app):
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)  # leftovers from a previous run
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=cleanup_loop, daemon=True).start()
    yield


app = FastAPI(title="AI Clipper API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_origin_regex=config.ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"], max_age=600)


class InfoRequest(BaseModel):
    url: str = Field(max_length=300)


class JobRequest(BaseModel):
    url: str = Field(max_length=300)
    clip_duration: Literal[15, 30, 45, 60, 90] = 30
    num_clips: int = Field(3, ge=1, le=10)
    captions: bool = True
    language: Literal["auto", "en", "si"] = "auto"
    ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    font_size: int = Field(100, ge=50, le=200)
    caption_position: Literal["top", "middle", "bottom"] = "bottom"
    caption_style: Literal["animated", "plain"] = "animated"
    rights_confirmed: bool = False


@app.get("/api/health")
def health():
    try:
        import whisper  # noqa: F401
        has_whisper = True
    except ImportError:
        has_whisper = False
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "whisper_installed": has_whisper,
            "whisper_model": config.WHISPER_MODEL, "max_video_minutes": config.MAX_VIDEO_MINUTES,
            "clips_kept_minutes": config.JOB_TTL_MINUTES}


@app.post("/api/info")
def info(req: InfoRequest, request: Request):
    ip = request.client.host if request.client else "?"
    hits = _info_hits[ip]
    now = time.time()
    while hits and hits[0] < now - 60:
        hits.popleft()
    if len(hits) >= config.INFO_RATE_PER_MIN:
        raise HTTPException(429, "Too many requests. Please wait a minute.")
    hits.append(now)
    try:
        url = pipeline.canonical_url(req.url)
        data = pipeline.get_info(url)
    except pipeline.UserError as e:
        raise HTTPException(400, str(e))
    data["too_long"] = data["duration"] > config.MAX_VIDEO_MINUTES * 60
    return data


def _public(job):
    with LOCK:
        queued_before = sum(1 for j in JOBS.values() if j["status"] == "queued" and j["created"] < job["created"])
        running = any(j["status"] == "running" for j in JOBS.values())
    pos = queued_before + (1 if running and job["status"] == "queued" else 0)
    jid = job["id"]
    clips = [{**c, "preview_url": f"/api/jobs/{jid}/clips/{c['index']}",
              "download_url": f"/api/jobs/{jid}/clips/{c['index']}?download=1",
              "srt_url": f"/api/jobs/{jid}/clips/{c['index']}/srt" if c["has_srt"] else None}
             for c in job["clips"]]
    return {"id": jid, "status": job["status"], "stage": job["stage"], "progress": job["progress"],
            "message": job["message"], "error": job["error"], "queue_position": pos, "clips": clips,
            "detected_language": job["language"], "expires_in_minutes": config.JOB_TTL_MINUTES}


def _worker(jid):
    job = JOBS.get(jid)
    if not job or job["status"] == "cancelled":
        return

    def update(stage, pct, msg):
        job.update(stage=stage, progress=pct, message=msg)

    job.update(status="running", stage="download", message="Starting…")
    try:
        res = pipeline.process(job["opts"], job["dir"], update, lambda: job["status"] == "cancelled")
        job.update(status="done", stage="done", progress=100, message="Your clips are ready.",
                   clips=res["clips"], language=res["language"])
    except pipeline.Cancelled:
        job.update(status="cancelled", message="Cancelled.")
        shutil.rmtree(job["dir"], ignore_errors=True)
    except pipeline.UserError as e:
        job.update(status="error", stage="error", error=str(e), message=str(e))
    except Exception:
        log.exception("job %s failed", jid)
        job.update(status="error", stage="error", error="Unexpected server error while processing. Try a shorter "
                   "video or a smaller Whisper model.", message="Processing failed.")
    finally:
        for f in job["dir"].glob("source.*"):
            f.unlink(missing_ok=True)
        for f in job["dir"].glob("chunk_*.wav"):
            f.unlink(missing_ok=True)


@app.post("/api/jobs", status_code=202)
def create_job(req: JobRequest):
    if not req.rights_confirmed:
        raise HTTPException(400, "Please confirm that you own this video or have permission to clip it.")
    try:
        pipeline.canonical_url(req.url)
    except pipeline.UserError as e:
        raise HTTPException(400, str(e))
    with LOCK:
        active = sum(1 for j in JOBS.values() if j["status"] in ("queued", "running"))
        if active >= config.MAX_ACTIVE_JOBS:
            raise HTTPException(429, "The server is busy with other jobs. Please try again in a few minutes.")
        jid = uuid.uuid4().hex
        d = config.WORK_DIR / jid
        d.mkdir(parents=True, exist_ok=True)
        JOBS[jid] = {"id": jid, "status": "queued", "stage": "queued", "progress": 0, "message": "Waiting in queue…",
                     "error": None, "clips": [], "language": None, "created": time.time(), "dir": d,
                     "opts": req.model_dump()}
    EXECUTOR.submit(_worker, jid)
    return {"job_id": jid}


def _job(jid):
    if not re.fullmatch(r"[0-9a-f]{32}", jid) or jid not in JOBS:
        raise HTTPException(404, "Job not found or expired.")
    return JOBS[jid]


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    return _public(_job(jid))


@app.delete("/api/jobs/{jid}")
def cancel_or_delete(jid: str):
    job = _job(jid)
    if job["status"] in ("queued", "running"):
        job["status"] = "cancelled"
        if job["stage"] == "queued":
            shutil.rmtree(job["dir"], ignore_errors=True)
    else:
        with LOCK:
            JOBS.pop(jid, None)
        shutil.rmtree(job["dir"], ignore_errors=True)
    return {"ok": True}


def _clip_path(jid, idx, ext):
    job = _job(jid)
    p = job["dir"] / f"clip_{idx}.{ext}"
    if job["status"] != "done" or not p.exists():
        raise HTTPException(404, "Clip not found.")
    return p


@app.get("/api/jobs/{jid}/clips/{idx}")
def get_clip(jid: str, idx: int, download: int = 0):
    p = _clip_path(jid, idx, "mp4")
    return FileResponse(p, media_type="video/mp4", filename=f"clip_{idx}.mp4" if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/jobs/{jid}/clips/{idx}/srt")
def get_srt(jid: str, idx: int):
    return FileResponse(_clip_path(jid, idx, "srt"), media_type="application/x-subrip", filename=f"clip_{idx}.srt")
