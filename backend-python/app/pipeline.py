"""Download -> audio -> Whisper transcription -> moment detection -> FFmpeg render."""
import gc
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import config
from .captions import RATIO_SIZE, clip_words, write_ass, write_srt
from .moments import find_moments

YT_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


class Cancelled(Exception):
    pass


class UserError(Exception):
    """Problem the user can understand/fix; message is shown in the UI."""


def video_id(url):
    m = YT_RE.match((url or "").strip())
    return m.group(1) if m else None


def canonical_url(url):
    vid = video_id(url)
    if not vid:
        raise UserError("That does not look like a supported YouTube link (watch, youtu.be or shorts).")
    return f"https://www.youtube.com/watch?v={vid}"  # never pass arbitrary URLs to yt-dlp


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-800:]}")
    return p


def _ydl_opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, **extra}
    if config.YTDLP_COOKIES_FILE and Path(config.YTDLP_COOKIES_FILE).exists():
        o["cookiefile"] = config.YTDLP_COOKIES_FILE
    return o


def _friendly(e):
    msg = str(e)
    if "Sign in" in msg or "not a bot" in msg or "confirm you" in msg:
        return ("YouTube blocked this server from reading the video (it often blocks cloud/datacenter IPs). "
                "See the README section 'YouTube blocking'.")
    if "private" in msg.lower() or "unavailable" in msg.lower():
        return "This video is private or unavailable."
    return "Could not read the video from YouTube: " + msg.splitlines()[-1][:200]


def get_info(url):
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            i = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise UserError(_friendly(e))
    return {"id": i.get("id"), "title": i.get("title"), "channel": i.get("uploader"),
            "duration": i.get("duration") or 0, "thumbnail": i.get("thumbnail"),
            "is_live": bool(i.get("is_live"))}


def download_video(url, job_dir, on_progress):
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            if tot:
                on_progress(min(1.0, d.get("downloaded_bytes", 0) / tot))

    opts = _ydl_opts(
        format="bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        outtmpl=str(job_dir / "source.%(ext)s"), merge_output_format="mp4", progress_hooks=[hook])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        raise UserError(_friendly(e))
    files = sorted(job_dir.glob("source.*"))
    if not files:
        raise UserError("Download finished but no video file was found.")
    return files[0]


def probe_duration(path):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.stdout.strip())


def extract_audio_chunks(video, job_dir):
    """16 kHz mono WAV, split into CHUNK_SECONDS pieces so memory stays small."""
    _run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
          "-f", "segment", "-segment_time", str(config.CHUNK_SECONDS), "-reset_timestamps", "1",
          str(job_dir / "chunk_%03d.wav")])
    chunks = sorted(job_dir.glob("chunk_*.wav"))
    if not chunks:
        raise UserError("This video has no audio track to transcribe.")
    return chunks


_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            import whisper  # local OpenAI Whisper (open source, runs on this server)
            _model = whisper.load_model(config.WHISPER_MODEL, device="cpu")
    return _model


def transcribe_chunks(chunks, language, on_progress, check):
    model = get_model()
    words, detected = [], None
    lang = None if language == "auto" else language
    for i, c in enumerate(chunks):
        check()
        res = model.transcribe(str(c), language=lang, word_timestamps=True, fp16=False,
                               condition_on_previous_text=False, verbose=None)
        if lang is None:
            lang = res.get("language")  # keep one language for the rest of the video
        detected = detected or res.get("language") or lang
        off = i * config.CHUNK_SECONDS
        for seg in res["segments"]:
            ws = seg.get("words") or []
            if not ws:  # fallback: spread words evenly over the segment
                toks = seg["text"].split()
                step = (seg["end"] - seg["start"]) / max(1, len(toks))
                ws = [{"word": t, "start": seg["start"] + k * step, "end": seg["start"] + (k + 1) * step}
                      for k, t in enumerate(toks)]
            for w in ws:
                t = w["word"].strip()
                if t and w["end"] > w["start"]:
                    words.append({"w": t, "s": w["start"] + off, "e": w["end"] + off})
        gc.collect()
        on_progress((i + 1) / len(chunks), i + 1, len(chunks))
    return words, detected


def render_clip(src, start, end, ratio, ass_name, out_name, cwd):
    """Cut, crop to the chosen ratio and burn captions. Runs inside the job folder so the
    subtitle filename stays simple (no path escaping problems)."""
    w, h = RATIO_SIZE[ratio]
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    if ass_name:
        vf += f",ass={ass_name}"
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
           "-threads", str(config.FFMPEG_THREADS), "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", out_name]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    if p.returncode != 0:
        raise RuntimeError("ffmpeg render failed: " + p.stderr[-800:])


def process(opts, job_dir, update, cancelled):
    """Runs the whole job. `update(stage, percent, message)` reports real progress."""
    def check():
        if cancelled():
            raise Cancelled()

    update("download", 2, "Reading video information…")
    url = canonical_url(opts["url"])
    info = get_info(url)
    if info["is_live"]:
        raise UserError("Live streams are not supported.")
    if info["duration"] > config.MAX_VIDEO_MINUTES * 60:
        raise UserError(f"This server accepts videos up to {config.MAX_VIDEO_MINUTES} minutes long.")
    check()
    video = download_video(url, job_dir, lambda f: update("download", 4 + int(26 * f), "Downloading video…"))
    check()
    update("audio", 32, "Extracting audio with FFmpeg…")
    total = probe_duration(video)
    chunks = extract_audio_chunks(video, job_dir)
    check()
    update("transcribe", 35, f"Loading Whisper '{config.WHISPER_MODEL}' model…")
    words, detected = transcribe_chunks(
        chunks, opts["language"],
        lambda f, d, n: update("transcribe", 35 + int(40 * f), f"Transcribing audio… (part {d} of {n})"), check)
    (job_dir / "transcript.json").write_text(json.dumps(words, ensure_ascii=False))
    for c in chunks:
        c.unlink(missing_ok=True)
    check()
    update("detect", 77, "Finding the best moments…")
    try:
        moments = find_moments(words, total, opts["clip_duration"], opts["num_clips"])
    except ValueError as e:
        raise UserError(str(e))
    clips = []
    for n, m in enumerate(moments, 1):
        check()
        update("render", 78 + int(20 * (n - 1) / len(moments)), f"Rendering clip {n} of {len(moments)}…")
        ass_name, has_srt = None, False
        if opts["captions"]:
            cw = clip_words(words, m["start"], m["end"])
            if write_ass(job_dir / f"clip_{n}.ass", cw, opts["ratio"], opts["font_size"],
                         opts["caption_position"], opts["caption_style"]):
                ass_name = f"clip_{n}.ass"
                write_srt(job_dir / f"clip_{n}.srt", cw, opts["ratio"])
                has_srt = True
        render_clip(video, m["start"], m["end"], opts["ratio"], ass_name, f"clip_{n}.mp4", job_dir)
        clips.append({"index": n, "start": m["start"], "end": m["end"],
                      "duration": round(m["end"] - m["start"], 1), "score": m["score"],
                      "title": m["title"], "reason": m["reason"], "has_srt": has_srt})
    video.unlink(missing_ok=True)  # free disk; only finished clips are kept
    return {"clips": clips, "language": detected, "info": info}
