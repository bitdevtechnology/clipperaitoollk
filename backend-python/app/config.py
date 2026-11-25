"""All settings come from environment variables (see .env.example)."""
import os
from pathlib import Path


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


# Websites allowed to call this API (your Netlify URL goes here), comma separated.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:8888,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080",
    ).split(",")
    if o.strip()
]
# Optional regex, handy for Netlify deploy previews, e.g. https://.*--my-site\.netlify\.app
ALLOWED_ORIGIN_REGEX = os.getenv("ALLOWED_ORIGIN_REGEX", "") or None

WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/clipper-jobs"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")  # tiny | base | small | medium
MAX_VIDEO_MINUTES = _int("MAX_VIDEO_MINUTES", 60)
JOB_TTL_MINUTES = _int("JOB_TTL_MINUTES", 60)  # finished jobs + files are deleted after this
CHUNK_SECONDS = _int("CHUNK_SECONDS", 300)  # audio is transcribed in chunks of this length
FFMPEG_THREADS = _int("FFMPEG_THREADS", 2)
MAX_ACTIVE_JOBS = _int("MAX_ACTIVE_JOBS", 3)  # queued + running
INFO_RATE_PER_MIN = _int("INFO_RATE_PER_MIN", 20)
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "")  # optional, server-side only
