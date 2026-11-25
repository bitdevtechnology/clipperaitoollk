"""Offline smoke test. Uses a generated local video + hand-written transcript instead of
YouTube and Whisper, but runs the REAL job queue, moment detection, caption generation and
FFmpeg rendering. It does NOT test yt-dlp downloads or Whisper accuracy.
Run:  python tests/smoke_test.py
"""
import shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
from app import config, main, pipeline

tmp = Path(tempfile.mkdtemp())
config.WORK_DIR = tmp / "jobs"
src = tmp / "demo.mp4"
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=100",
                "-f", "lavfi", "-i", "sine=frequency=300:duration=100", "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(src)], check=True, capture_output=True)

SENT = ["Here is the biggest mistake people make with money.", "Why do most people never save 20 percent?",
        "The truth is simple and it is free.", "Remember this one lesson today.",
        "Most days nothing special happens at all really."]
WORDS, t = [], 0.5
for i in range(40):
    for w in SENT[i % 5].split():
        WORDS.append({"w": w, "s": t, "e": t + 0.34}); t += 0.4
        if t > 98: break
    t += 0.3
    if t > 98: break

pipeline.get_info = lambda url: {"id": "x", "title": "demo", "channel": "c", "duration": 100, "thumbnail": None, "is_live": False}
pipeline.download_video = lambda url, d, cb: shutil.copy(src, d / "source.mp4") and d / "source.mp4"
pipeline.transcribe_chunks = lambda chunks, lang, prog, check: (WORDS, "en")

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
with TestClient(main.app) as c:
    assert c.get("/api/health").json()["ffmpeg"]
    assert c.post("/api/jobs", json={"url": "https://evil.com/x", "rights_confirmed": True}).status_code == 400
    assert c.post("/api/jobs", json={"url": URL, "rights_confirmed": False}).status_code == 400
    assert c.post("/api/jobs", json={"url": URL, "clip_duration": 33, "rights_confirmed": True}).status_code == 422
    assert c.get("/api/jobs/" + "0" * 32).status_code == 404
    r = c.options("/api/jobs", headers={"Origin": "http://localhost:8888", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:8888", r.headers
    r = c.options("/api/jobs", headers={"Origin": "https://evil.com", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    for ratio, caps, pos in [("9:16", True, "bottom"), ("16:9", True, "top"), ("1:1", False, "middle")]:
        body = dict(url=URL, clip_duration=30, num_clips=3, captions=caps, language="en", ratio=ratio,
                    caption_position=pos, font_size=110, rights_confirmed=True)
        jid = c.post("/api/jobs", json=body).json()["job_id"]
        for _ in range(120):
            s = c.get(f"/api/jobs/{jid}").json()
            if s["status"] in ("done", "error"): break
            time.sleep(1)
        assert s["status"] == "done", s
        assert len(s["clips"]) == 3
        for cl in s["clips"]:
            r = c.get(cl["download_url"]); assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
            assert (c.get(cl["preview_url"], headers={"Range": "bytes=0-99"}).status_code == 206)
            assert bool(cl["srt_url"]) == caps
        dst = tmp / f"{ratio.replace(':','x')}.mp4"; dst.write_bytes(c.get(s["clips"][0]["download_url"]).content)
        dims = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                               "-select_streams", "v", str(dst)], capture_output=True, text=True).stdout.strip()
        print(ratio, "captions" if caps else "no captions", "->", dims, [(x["start"], x["end"], x["score"], x["reason"]) for x in s["clips"]])
        if caps:
            print(c.get(s["clips"][0]["srt_url"]).text[:160])
            subprocess.run(["ffmpeg", "-y", "-ss", "3", "-i", str(dst), "-frames:v", "1", f"/tmp/frame_{ratio.replace(':','x')}.png"], capture_output=True)
        assert c.delete(f"/api/jobs/{jid}").status_code == 200
        assert not (config.WORK_DIR / jid).exists() and c.get(f"/api/jobs/{jid}").status_code == 404
print("ALL SMOKE TESTS PASSED")
