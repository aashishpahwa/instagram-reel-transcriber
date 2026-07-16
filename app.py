import csv
import glob
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
STORE_PATH = DATA_DIR / "store.json"
SETTINGS_PATH = DATA_DIR / "settings.json"

DOWNLOAD_WORKERS = 3
INSTAGRAM_URL_RE = re.compile(r"instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+", re.IGNORECASE)

DEFAULT_SETTINGS = {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": ""}

ANALYSIS_PROMPT = """You are analyzing the transcript of a short-form video (an Instagram Reel). Split it into four parts and respond with ONLY a strict JSON object with these exact keys: hook, promise, validation, cta.

- hook: the opening line(s) that grab attention
- promise: the build-up/tease of what's coming next (empty string "" if there isn't one)
- validation: the main body that delivers the content/value
- cta: the call to action at the end (empty string "" if there isn't one)

Use the transcript's own wording for each part verbatim - don't summarize, paraphrase, or add commentary. Every part of the transcript should be accounted for across the four fields, in order, with no overlap.

Transcript:
"""


def ensure_ffmpeg_on_path():
    """Make sure `ffmpeg` is callable. Falls back to the standard winget install
    location on Windows, since a just-installed PATH entry often isn't picked up
    by processes (like this one) that were already running when it was added."""
    if shutil.which("ffmpeg"):
        return
    if os.name == "nt":
        matches = glob.glob(os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe"
        ))
        if matches:
            os.environ["PATH"] = str(Path(matches[0]).parent) + os.pathsep + os.environ["PATH"]
            return
    sys.exit(
        "ffmpeg was not found on PATH. Install it (e.g. `winget install Gyan.FFmpeg` on Windows, "
        "`brew install ffmpeg` on macOS, or `apt install ffmpeg` on Linux), then restart your shell."
    )


ensure_ffmpeg_on_path()

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
store_lock = threading.Lock()
jobs_lock = threading.Lock()
jobs = {}  # id -> job dict; the live, authoritative state for this server process

download_queue = queue.Queue()
transcribe_queue = queue.Queue()

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper
            _whisper_model = whisper.load_model("small", device="cuda")
    return _whisper_model


def load_store():
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    return {}


def save_store(store):
    STORE_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def load_settings():
    if SETTINGS_PATH.exists():
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def format_timestamp(seconds):
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments_to_script(segments):
    if not segments:
        return ""
    return "\n".join(f"[{format_timestamp(seg['start'])}] {seg['text'].strip()}" for seg in segments)


def reel_id_for(url):
    match = re.search(r"instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def update_job(rid, **fields):
    with jobs_lock:
        jobs[rid].update(fields)


def new_job(rid, url, status="queued"):
    job = {"id": rid, "url": url, "status": status, "transcript": None, "segments": None,
           "video_file": None, "thumb_file": None, "meta": None, "analysis": None,
           "created_at": None, "error": None}
    with jobs_lock:
        jobs[rid] = job
    return job


def downloader_worker():
    while True:
        rid, url = download_queue.get()
        try:
            store = load_store()
            if rid in store and store[rid].get("status") == "done":
                with jobs_lock:
                    jobs[rid] = store[rid]
                continue

            update_job(rid, status="downloading")

            import yt_dlp
            ydl_opts = {
                "outtmpl": str(VIDEOS_DIR / f"{rid}.%(ext)s"),
                "format": "mp4/best",
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            requested = info.get("requested_downloads") or []
            video_path = Path(requested[0]["filepath"]) if requested else VIDEOS_DIR / f"{rid}.mp4"
            cdn_url = (requested[0].get("url") if requested else None) or info.get("url")

            meta = {
                "uploader": info.get("uploader"),
                "uploader_id": info.get("uploader_id"),
                "channel": info.get("channel"),
                "like_count": info.get("like_count"),
                "comment_count": info.get("comment_count"),
                "view_count": info.get("view_count"),
                "description": info.get("description"),
                "timestamp": info.get("timestamp"),
                "cdn_url": cdn_url,
            }

            thumb_path = VIDEOS_DIR / f"{rid}.jpg"
            thumb_file = None
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", "0.3", "-i", str(video_path), "-vframes", "1", "-update", "1", str(thumb_path)],
                    capture_output=True, timeout=20,
                )
                if thumb_path.exists():
                    thumb_file = thumb_path.name
            except Exception:
                pass

            update_job(rid, status="transcribing", meta=meta, thumb_file=thumb_file, video_file=video_path.name)
            transcribe_queue.put((rid, video_path))

        except Exception as exc:
            update_job(rid, status="error", error=str(exc))
        finally:
            download_queue.task_done()


def transcriber_worker():
    while True:
        rid, video_path = transcribe_queue.get()
        try:
            model = get_whisper_model()
            result = model.transcribe(str(video_path))
            transcript = result["text"].strip()
            segments = [{"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
                        for seg in result.get("segments", [])]

            update_job(rid, status="done", transcript=transcript, segments=segments,
                       created_at=datetime.now(timezone.utc).isoformat())

            with store_lock:
                store = load_store()
                store[rid] = jobs[rid]
                save_store(store)

        except Exception as exc:
            update_job(rid, status="error", error=str(exc))
        finally:
            transcribe_queue.task_done()


for _ in range(DOWNLOAD_WORKERS):
    threading.Thread(target=downloader_worker, daemon=True).start()
threading.Thread(target=transcriber_worker, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/jobs")
def all_jobs():
    store = load_store()
    with jobs_lock:
        merged = dict(store)
        merged.update(jobs)
        snapshot = list(merged.values())

    active = [j for j in snapshot if j.get("status") not in ("done", "error")]
    finished = sorted(
        [j for j in snapshot if j.get("status") in ("done", "error")],
        key=lambda j: j.get("created_at") or "", reverse=True,
    )
    return jsonify(active + finished)


def call_openai(settings, transcript):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        json={
            "model": settings["model"],
            "messages": [{"role": "user", "content": ANALYSIS_PROMPT + transcript}],
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def call_anthropic(settings, transcript):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/v1/messages",
        headers={
            "x-api-key": settings["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["model"],
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": ANALYSIS_PROMPT + transcript +
                          "\n\nRespond with ONLY the JSON object and nothing else - no markdown fencing, no commentary."}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["content"][0]["text"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    s = load_settings()
    return jsonify({"provider": s["provider"], "base_url": s["base_url"], "model": s["model"],
                     "has_key": bool(s.get("api_key"))})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True)
    s = load_settings()
    for field in ("provider", "base_url", "model"):
        if data.get(field):
            s[field] = data[field]
    if data.get("api_key"):
        s["api_key"] = data["api_key"]
    save_settings(s)
    return jsonify({"ok": True})


@app.route("/api/analyze/<rid>", methods=["POST"])
def analyze(rid):
    settings = load_settings()
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400

    with jobs_lock:
        job = jobs.get(rid)
    if not job:
        job = load_store().get(rid)
        if job:
            with jobs_lock:
                jobs[rid] = job

    if not job or job.get("status") != "done" or not job.get("transcript"):
        return jsonify({"error": "This reel isn't transcribed yet."}), 400

    try:
        if settings["provider"] == "anthropic":
            analysis = call_anthropic(settings, job["transcript"])
        else:
            analysis = call_openai(settings, job["transcript"])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    update_job(rid, analysis=analysis)
    with store_lock:
        store = load_store()
        store[rid] = jobs[rid]
        save_store(store)

    return jsonify(analysis)


@app.route("/api/export.csv")
def export_csv():
    include_link = request.args.get("link") == "1"
    include_metrics = request.args.get("metrics") == "1"
    include_timestamps = request.args.get("timestamps") == "1"
    include_analysis = request.args.get("analysis") == "1"

    store = load_store()
    with jobs_lock:
        merged = dict(store)
        merged.update(jobs)
        rows = [j for j in merged.values() if j.get("status") == "done"]

    rows.sort(key=lambda j: j.get("created_at") or "", reverse=True)

    fieldnames = ["url", "uploader", "transcript"]
    if include_link:
        fieldnames.append("video_cdn_link")
    if include_metrics:
        fieldnames += ["likes", "comments", "views"]
    if include_timestamps:
        fieldnames.append("timestamped_script")
    if include_analysis:
        fieldnames += ["hook", "promise", "validation", "cta"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for j in rows:
        m = j.get("meta") or {}
        row = {
            "url": j.get("url", ""),
            "uploader": m.get("channel") or m.get("uploader") or "",
            "transcript": j.get("transcript") or "",
        }
        if include_link:
            row["video_cdn_link"] = m.get("cdn_url") or ""
        if include_metrics:
            row["likes"] = m.get("like_count")
            row["comments"] = m.get("comment_count")
            row["views"] = m.get("view_count")
        if include_timestamps:
            row["timestamped_script"] = segments_to_script(j.get("segments"))
        if include_analysis:
            a = j.get("analysis") or {}
            row["hook"] = a.get("hook", "")
            row["promise"] = a.get("promise", "")
            row["validation"] = a.get("validation", "")
            row["cta"] = a.get("cta", "")
        writer.writerow(row)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=reel_transcripts.csv"},
    )


@app.route("/api/submit", methods=["POST"])
def submit():
    data = request.get_json(force=True)
    raw_lines = data.get("urls", [])
    lines = [u.strip() for u in raw_lines if u.strip()]

    accepted_ids = []
    skipped = []

    for line in lines:
        if not INSTAGRAM_URL_RE.search(line):
            skipped.append(line)
            continue
        rid = reel_id_for(line)
        with jobs_lock:
            already_running = rid in jobs and jobs[rid].get("status") not in ("done", "error")
        if rid not in jobs or not already_running:
            new_job(rid, line, status="queued")
            download_queue.put((rid, line))
        accepted_ids.append(rid)

    return jsonify({"accepted": accepted_ids, "skipped": skipped})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5151, debug=False)
