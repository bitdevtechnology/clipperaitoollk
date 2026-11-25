# ClipForge – AI YouTube Auto Clipper (Netlify frontend + Python backend)

Two separate projects:

| Folder | What it is | Where it runs |
|---|---|---|
| `frontend-netlify/` | The website (HTML, CSS, JavaScript). No secrets. | Netlify |
| `backend-python/` | FastAPI server: yt-dlp, FFmpeg, local OpenAI Whisper | A Docker host (Render, Hugging Face Spaces, Railway, Fly.io, your own VPS) |

The website only talks to the backend through a REST API. Netlify never runs Python, FFmpeg or Whisper.

## What was tested, and what was NOT

Tested in the build environment (real code, real FFmpeg):
- Backend API: URL validation, permission checkbox, input validation, CORS allow/deny, job queue, status polling, cancel and cleanup, MP4 streaming (HTTP Range) and forced download.
- Moment detection, SRT generation, animated ASS captions (yellow active word, orange keywords, position and size options) burned in with FFmpeg, in 9:16, 16:9 and 1:1 (checked visually and with ffprobe).
- Frontend in a real browser (Playwright): URL check, video info, options, progress, clip preview, download, no console errors.
- These tests used a generated video and a hand-written transcript in place of YouTube and Whisper. Run `python tests/smoke_test.py` inside `backend-python/` to repeat them.

NOT tested here (the sandbox could not reach YouTube or download Whisper models):
- Real YouTube downloads with yt-dlp.
- Real Whisper transcription, including Sinhala accuracy.
- A real deployment on Netlify and a backend host.

So treat the full application as "built, partially verified". Your first real run on your own deployment is the final test. Tell me about any errors and I will help fix them.

## How it works

1. You paste a link. The backend checks it and reads the title, channel and length.
2. Generate clips creates a job and returns a job ID. A background worker processes one video at a time.
3. The worker downloads the video (max 720p), extracts 16 kHz mono audio and splits it into 5-minute chunks, so memory use stays small on long videos.
4. Whisper (running on the backend, free, no API key) transcribes each chunk with word timestamps.
5. **Moment detection** is a heuristic, not an LLM. It scores every stretch of speech that fits your clip length. It looks at hook keywords, questions, numbers, speaking pace, vocabulary variety and clean sentence start/end. It then picks the best non-overlapping stretches. It works best on spoken English and is weaker for Sinhala, because the keyword list is English.
6. FFmpeg cuts each clip, crops it to your ratio (centre crop) and burns in the captions. An SRT file is also created.
7. The website polls `/api/jobs/{id}` every 2 seconds and shows the real stage and percentage.
8. Clips and files are deleted `JOB_TTL_MINUTES` after the job starts (default 60).

Transcription always runs, even with captions OFF, because moment detection needs the transcript.

## Limits you should know about

- **YouTube blocking.** YouTube often blocks downloads from cloud/datacenter IP addresses ("confirm you're not a bot"). This is the biggest risk for any hosted downloader. Options: try another host, run the backend on your own computer or a VPS, or use videos you own. Cookies (`YTDLP_COOKIES_FILE`) sometimes help, but they come from your account. Use them only for your own videos and read YouTube's terms first.
- **Terms of Service.** Downloading videos may break YouTube's Terms unless you own the video or have permission. The site makes you confirm permission, but you are responsible for it.
- **Free hosting is weak for Whisper.** Rough RAM guide: `tiny` about 1 GB, `base` about 1.5 GB, `small` about 3 GB. Render's free plan (512 MB) is too small. Hugging Face Spaces (free CPU, 16 GB RAM) can fit it but is slow and may sleep. Expect transcription to take 1x to 3x the video length on a small CPU.
- **Sinhala.** Whisper supports Sinhala (`si`), but accuracy is much lower than English. Use `WHISPER_MODEL=small` or larger. Caption font: Noto Sans Sinhala.
- **Storage is temporary.** Files live on the server disk and vanish on restart. Job status is kept in memory, so a restart loses running jobs.
- **Single worker.** One video at a time; others wait in the queue (up to `MAX_ACTIVE_JOBS`).
- **Auto-crop.** 9:16 is a centre crop, so side content is cut off.
- I cannot promise the whole system is "free": the code and Whisper are free, but hosting with enough RAM may cost money.

## Step-by-step deployment (beginner friendly)

### Step 0 – Accounts and tools
Create free accounts on [GitHub](https://github.com) and [Netlify](https://netlify.com), and one on a backend host (Step 2). Install [Git](https://git-scm.com).

### Step 1 – Put the code on GitHub
1. On GitHub click **New repository**, name it `clipforge`, click **Create**.
2. Open a terminal in this project's top folder (the one containing both folders) and run:
```
git init
git add .
git commit -m "first version"
git branch -M main
git remote add origin https://github.com/YOUR-NAME/clipforge.git
git push -u origin main
```

### Step 2 – Deploy the backend

**Option A: Render (simple, needs a paid plan with at least 2 GB RAM)**
1. On [render.com](https://render.com): **New → Web Service →** connect your GitHub repo.
2. Root Directory: `backend-python`. Runtime: **Docker**. Pick a plan with enough RAM.
3. Under **Environment** add `ALLOWED_ORIGINS` = your Netlify address (you get it in Step 3, so come back and set it) and `WHISPER_MODEL` = `base`.
4. Deploy. The first build takes 10+ minutes (it installs PyTorch and downloads the Whisper model).
5. Open `https://YOUR-SERVICE.onrender.com/api/health`. You should see `"ffmpeg":true,"whisper_installed":true`.

**Option B: Hugging Face Spaces (free CPU)**
1. On huggingface.co create a **New Space** with SDK = **Docker**.
2. Upload the *contents* of `backend-python/` (the `Dockerfile` must be at the top). Add a `README.md` at the top of the Space starting with:
```
---
title: clipforge-api
sdk: docker
app_port: 7860
---
```
3. In **Settings → Variables** add `ALLOWED_ORIGINS`.
4. Your API address looks like `https://YOUR-NAME-clipforge-api.hf.space`.

### Step 3 – Deploy the frontend on Netlify
1. Open `frontend-netlify/config.js` and replace the address with your backend's `https://…` address (no trailing slash). Commit and push to GitHub.
2. On Netlify: **Add new site → Import an existing project → GitHub →** choose the repo.
3. Set **Base directory** = `frontend-netlify`. Leave the build command empty. Publish directory: `.`
4. Click **Deploy**. You get an address like `https://something.netlify.app`.
5. On your backend host set `ALLOWED_ORIGINS=https://something.netlify.app` (exactly, no trailing slash), then restart it.
6. Open your Netlify site. The top-right badge should say **Server online · Whisper base**.

### Step 4 – First real test
Use a short video (under 5 minutes) that you own. Click Check video, then Generate clips. If it fails, the red message explains why.

## Run it on your own computer first (optional)
```
# backend (needs Python 3.10+ and FFmpeg installed)
cd backend-python
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000

# frontend, in a second terminal
cd frontend-netlify
python -m http.server 8080       # then open http://localhost:8080
```
Local CORS already allows `http://localhost:8080`.

## Settings
See `backend-python/.env.example`. The frontend needs only `API_BASE_URL` in `config.js`, which is a public address, not a secret. There are no API keys anywhere.

## Troubleshooting
- **"Cannot reach the server"**: wrong `API_BASE_URL`, the host is asleep (wait a minute), or `ALLOWED_ORIGINS` does not exactly match your Netlify address.
- **"YouTube blocked this server"**: see "YouTube blocking" above.
- **Job fails after a long time / "Unexpected server error"**: probably out of memory. Use `WHISPER_MODEL=tiny`, a shorter video, or a bigger server.
- **Sinhala captions look wrong**: use `WHISPER_MODEL=small` or `medium`, and pick Sinhala instead of auto-detect.
