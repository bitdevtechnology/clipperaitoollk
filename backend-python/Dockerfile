FROM python:3.11-slim
ARG WHISPER_MODEL=base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f
RUN useradd -m -u 1000 app
WORKDIR /home/app
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt
USER app
ENV HOME=/home/app WORK_DIR=/tmp/clipper-jobs WHISPER_MODEL=${WHISPER_MODEL}
# Download the Whisper model now so the first request is not slow
RUN python -c "import whisper,os; whisper.load_model(os.environ['WHISPER_MODEL'])"
COPY --chown=app:app app ./app
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
