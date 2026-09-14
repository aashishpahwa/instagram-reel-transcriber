# Cloud image: no GPU, no local Whisper. Transcription falls back to Groq's
# hosted Whisper automatically (see transcribe_audio in app.py).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: yt-dlp merges/remuxes with it, storyboard frames and the Groq audio
# extract are plain ffmpeg calls, and app.py refuses to start without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt ./
RUN pip install -r requirements-server.txt

COPY . .

# Downloaded reels, thumbnails, storyboard frames and cookies.txt live here;
# mount a volume so they survive redeploys.
VOLUME ["/app/data"]

EXPOSE 5151

# Exactly ONE worker process: the download/transcribe/analysis queues and the
# live `jobs` table are in-process state, and a second worker would have its
# own copy of all of it. Concurrency comes from threads instead.
CMD ["gunicorn", "--bind", "0.0.0.0:5151", "--workers", "1", "--threads", "16", \
     "--timeout", "600", "--graceful-timeout", "30", "--access-logfile", "-", "app:app"]
