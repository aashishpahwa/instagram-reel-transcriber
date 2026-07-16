import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
STORE_PATH = DATA_DIR / "store.json"


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
jobs = {}  # id -> job dict, in-memory status for the current run (includes done items merged from store)

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


def reel_id_for(url):
    match = re.search(r"instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def process_url(url):
    rid = reel_id_for(url)
    store = load_store()

    if rid in store and store[rid].get("status") == "done":
        jobs[rid] = store[rid]
        return

    job = {"id": rid, "url": url, "status": "downloading", "transcript": None, "video_file": None,
           "thumb_file": None, "meta": None, "created_at": None, "error": None}
    jobs[rid] = job

    try:
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

        job["meta"] = {
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "channel": info.get("channel"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "view_count": info.get("view_count"),
            "description": info.get("description"),
            "timestamp": info.get("timestamp"),
        }

        thumb_path = VIDEOS_DIR / f"{rid}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "0.3", "-i", str(video_path), "-vframes", "1", "-update", "1", str(thumb_path)],
                capture_output=True, timeout=20,
            )
            if thumb_path.exists():
                job["thumb_file"] = thumb_path.name
        except Exception:
            pass

        job["status"] = "transcribing"
        model = get_whisper_model()
        result = model.transcribe(str(video_path))
        transcript = result["text"].strip()

        job["status"] = "done"
        job["transcript"] = transcript
        job["video_file"] = video_path.name
        job["created_at"] = datetime.now(timezone.utc).isoformat()

        with store_lock:
            store = load_store()
            store[rid] = job
            save_store(store)

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


def process_urls_async(urls):
    for url in urls:
        try:
            process_url(url)
        except Exception as exc:
            rid = reel_id_for(url)
            jobs[rid] = {"id": rid, "url": url, "status": "error", "transcript": None, "video_file": None, "meta": None, "error": str(exc)}


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/history")
def history():
    store = load_store()
    items = sorted(store.values(), key=lambda j: j.get("created_at") or "", reverse=True)
    return jsonify(items)


@app.route("/api/submit", methods=["POST"])
def submit():
    data = request.get_json(force=True)
    raw_urls = data.get("urls", [])
    urls = [u.strip() for u in raw_urls if u.strip()]
    ids = [reel_id_for(u) for u in urls]

    for rid, url in zip(ids, urls):
        if rid not in jobs:
            store = load_store()
            if rid in store:
                jobs[rid] = store[rid]
            else:
                jobs[rid] = {"id": rid, "url": url, "status": "queued", "transcript": None, "video_file": None,
                             "thumb_file": None, "meta": None, "created_at": None, "error": None}

    thread = threading.Thread(target=process_urls_async, args=(urls,), daemon=True)
    thread.start()

    return jsonify(ids)


@app.route("/api/status")
def status():
    ids = request.args.get("ids", "")
    id_list = [i for i in ids.split(",") if i]
    return jsonify([jobs.get(i) for i in id_list if jobs.get(i)])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5151, debug=False)
