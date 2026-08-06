import collections
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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
DATABASE_URL = os.environ["DATABASE_URL"]

MIN_BULK_REELS = 6

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

FORMULA_ANALYSIS_PROMPT_TEMPLATE = """You are maintaining a persistent library of reusable content formulas extracted from a batch of short-form video (Instagram Reel) transcripts that have each already been split into four parts: hook, promise, validation, cta. Each reel also comes with engagement data (likes, comments) and computed delivery metrics (words per minute, sentence length, filler-word density, direct-address density, question count, number/stat mentions, hook length, and how far into the video the CTA lands).

Reels are grouped into two engagement tiers based on a likes+comments composite score: "high" (top third of this batch) and "low" (bottom third). This score is a proxy only - Instagram view counts aren't available, so a low score can also just mean low reach rather than a bad reel. Say so explicitly in your caveats.

Your job: find recurring formulas - reusable patterns in how the hook opens, how the script is structured (e.g. problem-agitate-solve, listicle, myth-bust, before/after, personal story), and how the creator talks (pacing, directness, energy) - phrased like "this person did X" reusable templates, not one-off observations. Only use these category slugs: {categories}.

You are extending an EXISTING library, not starting fresh. Existing formulas already recorded (id, category, name, description):
{existing_formulas}

For each pattern you find in this batch: if it represents the SAME underlying idea as one already listed above (even if worded differently), reuse that formula_id and return updated rating/reason/evidence rather than creating a near-duplicate - bias toward merging. Only propose a new formula (formula_id omitted) if it's genuinely distinct from everything listed above. Never invent a formula_id that isn't in the list above.

Respond with ONLY a strict JSON object with these exact keys:
- matched: array of {{"formula_id": <int from the list above>, "category": one of {categories}, "rating": 1-5, "status": "working"|"not_working", "reason": why, updated for this run, "evidence_reel_ids": [ids from this batch]}}
- new: array of {{"category": one of {categories}, "name": short reusable label, "description": the reusable pattern itself, "reason": why it likely works or doesn't, "rating": 1-5, "status": "working"|"not_working", "evidence_reel_ids": [ids from this batch]}}
- summary: 2-4 sentence plain-English takeaway for this run
- caveats: 1-3 sentences on the limits of this analysis (proxy metric, sample size, anything else relevant)

Batch data for this run (JSON):
{batch}
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
jobs_lock = threading.Lock()
jobs = {}  # id -> job dict; the live, authoritative state for this server process

download_queue = queue.Queue()
transcribe_queue = queue.Queue()
analysis_queue = queue.Queue()
link_queue = queue.Queue()

_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper
            _whisper_model = whisper.load_model("small", device="cuda")
    return _whisper_model


# ---------- Database ----------
# jsonb columns come back as native dicts/lists without manual json.loads.
psycopg2.extras.register_default_jsonb(globally=True)
psycopg2.extras.register_default_json(globally=True)

db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=15, dsn=DATABASE_URL)

REEL_COLUMNS = ["id", "url", "status", "transcript", "segments", "video_file", "thumb_file",
                "created_at", "meta", "analysis", "error"]


def _get_live_conn():
    """Neon can suspend-and-drop idle connections; a pooled connection can go
    stale between requests. Probe before handing it out rather than discovering
    that mid-transaction."""
    while True:
        conn = db_pool.getconn()
        try:
            with conn.cursor() as probe:
                probe.execute("SELECT 1")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            db_pool.putconn(conn, close=True)


@contextmanager
def db_cursor():
    conn = _get_live_conn()
    close_conn = False
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            close_conn = True
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        db_pool.putconn(conn, close=close_conn)


# In-memory mirrors of the `reels`/`settings` tables, so the frequently-polled
# /api/jobs route (every 1.2s while anything is active) never hits the DB directly.
store_cache = {}
store_cache_lock = threading.Lock()
settings_cache = {}
settings_cache_lock = threading.Lock()

_reel_locks = collections.defaultdict(threading.Lock)
_reel_locks_guard = threading.Lock()


def _lock_for(rid):
    with _reel_locks_guard:
        return _reel_locks[rid]


def load_store():
    """DB read of every reel - only ever called once, at startup, to seed store_cache."""
    with db_cursor() as cur:
        cur.execute(f"SELECT {', '.join(REEL_COLUMNS)} FROM reels")
        result = {}
        for row in cur.fetchall():
            d = dict(row)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()  # keep the same ISO-string shape jobs/JSON use everywhere else
            result[d["id"]] = d
        return result


def upsert_reel(job):
    row = {col: job.get(col) for col in REEL_COLUMNS}
    if row.get("created_at"):
        row["created_at"] = datetime.fromisoformat(row["created_at"])  # explicit parse, not implicit string coercion
    row["segments"] = psycopg2.extras.Json(row["segments"])
    row["meta"] = psycopg2.extras.Json(row["meta"])
    row["analysis"] = psycopg2.extras.Json(row["analysis"])
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO reels (id, url, status, transcript, segments, video_file, thumb_file,
                               created_at, meta, analysis, error)
            VALUES (%(id)s, %(url)s, %(status)s, %(transcript)s, %(segments)s, %(video_file)s,
                    %(thumb_file)s, %(created_at)s, %(meta)s, %(analysis)s, %(error)s)
            ON CONFLICT (id) DO UPDATE SET
                url = EXCLUDED.url, status = EXCLUDED.status, transcript = EXCLUDED.transcript,
                segments = EXCLUDED.segments, video_file = EXCLUDED.video_file,
                thumb_file = EXCLUDED.thumb_file, meta = EXCLUDED.meta,
                analysis = EXCLUDED.analysis, error = EXCLUDED.error
            """,
            row,
        )


def persist_reel(rid):
    """Snapshot the in-memory job and write it through to the DB + store_cache,
    serialized per reel id so a worker thread and a manual re-trigger touching
    the same reel can't blind-overwrite each other."""
    with _lock_for(rid):
        with jobs_lock:
            snapshot = dict(jobs[rid])
        upsert_reel(snapshot)
        with store_cache_lock:
            store_cache[rid] = snapshot
    return snapshot


def load_settings():
    with settings_cache_lock:
        if settings_cache:
            return dict(settings_cache)
    with db_cursor() as cur:
        cur.execute("SELECT provider, base_url, model, api_key FROM settings WHERE id = 1")
        row = cur.fetchone()
    s = {**DEFAULT_SETTINGS, **(dict(row) if row else {})}
    with settings_cache_lock:
        settings_cache.update(s)
    return dict(s)


def save_settings(settings):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO settings (id, provider, base_url, model, api_key)
            VALUES (1, %(provider)s, %(base_url)s, %(model)s, %(api_key)s)
            ON CONFLICT (id) DO UPDATE SET
                provider = EXCLUDED.provider, base_url = EXCLUDED.base_url,
                model = EXCLUDED.model, api_key = EXCLUDED.api_key
            """,
            settings,
        )
    with settings_cache_lock:
        settings_cache.update(settings)


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


FILLER_WORDS_RE = re.compile(r"\b(um+|uh+|like|basically|actually|literally|you know|kind of|sort of)\b", re.IGNORECASE)
YOU_RE = re.compile(r"\b(you|your|you're|youre)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def compute_metrics(job):
    """Pure function over a job's existing fields - nothing here is persisted to the
    store, it's cheap enough to recompute per request."""
    transcript = (job.get("transcript") or "").strip()
    segments = job.get("segments") or []
    analysis = job.get("analysis") or {}
    meta = job.get("meta") or {}

    word_count = len(transcript.split())
    duration_sec = segments[-1]["end"] if segments else None
    wpm = round(word_count / (duration_sec / 60), 1) if duration_sec else None

    sentences = [s for s in SENTENCE_SPLIT_RE.split(transcript) if s.strip()]
    sentence_count = len(sentences)
    avg_sentence_len = round(word_count / sentence_count, 1) if sentence_count else None

    filler_count = len(FILLER_WORDS_RE.findall(transcript))
    you_count = len(YOU_RE.findall(transcript))

    hook_word_count = None
    cta_position_pct = None
    if analysis:
        hook_text = analysis.get("hook") or ""
        hook_word_count = len(hook_text.split())
        pre_cta_len = len(hook_text) + len(analysis.get("promise") or "") + len(analysis.get("validation") or "")
        cta_position_pct = round(pre_cta_len / len(transcript) * 100, 1) if transcript else None

    like_count = meta.get("like_count") or 0
    comment_count = meta.get("comment_count") or 0

    return {
        "duration_sec": duration_sec,
        "word_count": word_count,
        "wpm": wpm,
        "sentence_count": sentence_count,
        "avg_sentence_len": avg_sentence_len,
        "question_count": transcript.count("?"),
        "filler_per_1k": round(filler_count / word_count * 1000, 1) if word_count else None,
        "you_density": round(you_count / word_count * 100, 1) if word_count else None,
        "number_mentions": len(NUMBER_RE.findall(transcript)),
        "hook_word_count": hook_word_count,
        "cta_position_pct": cta_position_pct,
        "like_count": like_count,
        "comment_count": comment_count,
        "engagement_score": like_count + comment_count,
    }


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
            with store_cache_lock:
                cached = store_cache.get(rid)
            if cached and cached.get("status") == "done":
                with jobs_lock:
                    jobs[rid] = cached
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
            transcribe_queue.put(("new", rid, video_path))

        except Exception as exc:
            update_job(rid, status="error", error=str(exc))
        finally:
            download_queue.task_done()


def transcriber_worker():
    while True:
        kind, rid, video_path = transcribe_queue.get()
        try:
            model = get_whisper_model()
            result = model.transcribe(str(video_path))
            transcript = result["text"].strip()
            segments = [{"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
                        for seg in result.get("segments", [])]

            if kind == "new":
                update_job(rid, status="done", transcript=transcript, segments=segments,
                           created_at=datetime.now(timezone.utc).isoformat())
            else:  # backfill: reel is already done, just fill in segments
                update_job(rid, segments=segments)

            persist_reel(rid)

        except Exception as exc:
            print(f"[transcriber_worker] {kind} failed for {rid}: {exc}", file=sys.stderr)
            if kind == "new":
                update_job(rid, status="error", error=str(exc))
        finally:
            transcribe_queue.task_done()


def analysis_worker():
    while True:
        rid = analysis_queue.get()
        try:
            settings = load_settings()
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("transcript") and settings.get("api_key") and not job.get("analysis"):
                analysis = call_ai(settings, ANALYSIS_PROMPT + job["transcript"])
                update_job(rid, analysis=analysis)
                persist_reel(rid)
        except Exception as exc:
            print(f"[analysis_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            analysis_queue.task_done()


def link_worker():
    while True:
        rid = link_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("url"):
                import yt_dlp
                ydl_opts = {"format": "mp4/best", "quiet": True, "no_warnings": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(job["url"], download=False)
                cdn_url = info.get("url")
                if cdn_url:
                    meta = dict(job.get("meta") or {})
                    meta["cdn_url"] = cdn_url
                    update_job(rid, meta=meta)
                    persist_reel(rid)
        except Exception as exc:
            print(f"[link_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            link_queue.task_done()


store_cache = load_store()
load_settings()  # seed settings_cache

for _ in range(DOWNLOAD_WORKERS):
    threading.Thread(target=downloader_worker, daemon=True).start()
threading.Thread(target=transcriber_worker, daemon=True).start()
for _ in range(2):
    threading.Thread(target=analysis_worker, daemon=True).start()
for _ in range(3):
    threading.Thread(target=link_worker, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/jobs")
def all_jobs():
    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)
        snapshot = list(merged.values())

    active = [j for j in snapshot if j.get("status") not in ("done", "error")]
    finished = sorted(
        [j for j in snapshot if j.get("status") in ("done", "error")],
        key=lambda j: j.get("created_at") or "", reverse=True,
    )
    return jsonify(active + finished)


def call_openai(settings, prompt):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        json={
            "model": settings["model"],
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def call_anthropic(settings, prompt, max_tokens=2048):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/v1/messages",
        headers={
            "x-api-key": settings["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt +
                          "\n\nRespond with ONLY the JSON object and nothing else - no markdown fencing, no commentary."}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["content"][0]["text"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def call_ai(settings, prompt, max_tokens=2048):
    if settings["provider"] == "anthropic":
        return call_anthropic(settings, prompt, max_tokens=max_tokens)
    return call_openai(settings, prompt)


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
        with store_cache_lock:
            job = store_cache.get(rid)
        if job:
            with jobs_lock:
                jobs[rid] = job

    if not job or job.get("status") != "done" or not job.get("transcript"):
        return jsonify({"error": "This reel isn't transcribed yet."}), 400

    try:
        analysis = call_ai(settings, ANALYSIS_PROMPT + job["transcript"])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    update_job(rid, analysis=analysis)
    persist_reel(rid)

    return jsonify(analysis)


@app.route("/api/export/prepare", methods=["POST"])
def export_prepare():
    data = request.get_json(force=True)
    need_timestamps = bool(data.get("timestamps"))
    need_analysis = bool(data.get("analysis"))
    need_link = bool(data.get("link"))

    settings = load_settings()
    has_key = bool(settings.get("api_key"))

    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)
        done_jobs = [j for j in merged.values() if j.get("status") == "done"]

    pending = set()
    for j in done_jobs:
        rid = j["id"]
        needs_ts = need_timestamps and not j.get("segments") and j.get("video_file")
        needs_analysis = need_analysis and has_key and not j.get("analysis") and j.get("transcript")
        needs_link = need_link and not (j.get("meta") or {}).get("cdn_url") and j.get("url")
        if not (needs_ts or needs_analysis or needs_link):
            continue

        with jobs_lock:
            if rid not in jobs:
                jobs[rid] = j

        if needs_link:
            link_queue.put(rid)
            pending.add(rid)

        if needs_ts:
            video_path = VIDEOS_DIR / j["video_file"]
            if video_path.exists():
                transcribe_queue.put(("backfill", rid, video_path))
                pending.add(rid)
        if needs_analysis:
            analysis_queue.put(rid)
            pending.add(rid)

    return jsonify({
        "pending_ids": sorted(pending),
        "needs_key": need_analysis and not has_key,
    })


@app.route("/api/export.csv")
def export_csv():
    include_link = request.args.get("link") == "1"
    include_metrics = request.args.get("metrics") == "1"
    include_timestamps = request.args.get("timestamps") == "1"
    include_analysis = request.args.get("analysis") == "1"

    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
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


def _done_jobs():
    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)
        return [j for j in merged.values() if j.get("status") == "done"]


def _jsonify_row(row):
    if row is None:
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _tiered_rows():
    """All done reels with an AI breakdown, ranked by engagement_score and split
    into high/mid/low thirds. Recomputed live on every call - tiers are never
    persisted since the split depends on the whole current batch."""
    done_jobs = [j for j in _done_jobs() if j.get("analysis")]
    rows = [{"job": j, "metrics": compute_metrics(j)} for j in done_jobs]
    rows.sort(key=lambda r: r["metrics"]["engagement_score"], reverse=True)

    n = len(rows)
    tier_size = max(1, n // 3)
    for i, r in enumerate(rows):
        if i < tier_size:
            r["tier"] = "high"
        elif i >= n - tier_size:
            r["tier"] = "low"
        else:
            r["tier"] = "mid"
    return rows


@app.route("/api/analysis/categories")
def analysis_categories():
    with db_cursor() as cur:
        cur.execute("SELECT slug, display_name FROM formula_categories ORDER BY sort_order")
        return jsonify(cur.fetchall())


@app.route("/api/analysis/overview")
def analysis_overview():
    rows = _tiered_rows()
    table = [
        {
            "id": r["job"]["id"],
            "title": (r["job"].get("meta") or {}).get("channel") or (r["job"].get("meta") or {}).get("uploader") or r["job"]["id"],
            "thumb_file": r["job"].get("thumb_file"),
            "tier": r["tier"],
            **r["metrics"],
        }
        for r in rows
    ]
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, generated_at, high_count, low_count, mid_count, summary, caveats "
            "FROM analysis_runs ORDER BY generated_at DESC LIMIT 1"
        )
        run = cur.fetchone()
    return jsonify({"run": _jsonify_row(run), "table": table})


@app.route("/api/analysis/formulas")
def analysis_formulas():
    category = request.args.get("category")
    query = """
        SELECT f.*, COALESCE(array_agg(fe.reel_id) FILTER (WHERE fe.reel_id IS NOT NULL), '{}') AS evidence_reel_ids
        FROM formulas f LEFT JOIN formula_evidence fe ON fe.formula_id = f.id
        __WHERE__
        GROUP BY f.id
        ORDER BY f.category, f.rating DESC NULLS LAST, f.times_seen DESC
    """
    with db_cursor() as cur:
        if category:
            cur.execute(query.replace("__WHERE__", "WHERE f.category = %s"), (category,))
        else:
            cur.execute(query.replace("__WHERE__", ""))
        rows = cur.fetchall()
    return jsonify([_jsonify_row(r) for r in rows])


@app.route("/api/analysis/prepare", methods=["POST"])
def analysis_prepare():
    settings = load_settings()
    has_key = bool(settings.get("api_key"))
    done_jobs = _done_jobs()

    pending = set()
    for j in done_jobs:
        rid = j["id"]
        if has_key and not j.get("analysis") and j.get("transcript"):
            with jobs_lock:
                if rid not in jobs:
                    jobs[rid] = j
            analysis_queue.put(rid)
            pending.add(rid)

    return jsonify({
        "pending_ids": sorted(pending),
        "needs_key": not has_key,
        "eligible_count": len(done_jobs),
    })


@app.route("/api/analysis/generate", methods=["POST"])
def analysis_generate():
    settings = load_settings()
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400

    rows = _tiered_rows()
    if len(rows) < MIN_BULK_REELS:
        return jsonify({
            "error": f"Need at least {MIN_BULK_REELS} reels with an AI breakdown to generate formulas "
                     f"(have {len(rows)})."
        }), 400

    high_count = sum(1 for r in rows if r["tier"] == "high")
    low_count = sum(1 for r in rows if r["tier"] == "low")
    mid_count = len(rows) - high_count - low_count
    valid_reel_ids = {r["job"]["id"] for r in rows if r["tier"] in ("high", "low")}

    batch_payload = [
        {
            "id": r["job"]["id"],
            "tier": r["tier"],
            "metrics": {k: v for k, v in r["metrics"].items() if k != "engagement_score"},
            "hook": r["job"]["analysis"].get("hook", ""),
            "promise": r["job"]["analysis"].get("promise", ""),
            "validation": r["job"]["analysis"].get("validation", ""),
            "cta": r["job"]["analysis"].get("cta", ""),
        }
        for r in rows if r["tier"] in ("high", "low")
    ]

    with db_cursor() as cur:
        cur.execute("SELECT slug FROM formula_categories ORDER BY sort_order")
        category_slugs = [row["slug"] for row in cur.fetchall()]
        cur.execute("SELECT id, category, name, description FROM formulas")
        existing = cur.fetchall()

    existing_by_id = {row["id"]: row for row in existing}
    existing_json = [{"id": row["id"], "category": row["category"], "name": row["name"],
                       "description": row["description"]} for row in existing]

    prompt = FORMULA_ANALYSIS_PROMPT_TEMPLATE.format(
        categories=json.dumps(category_slugs),
        existing_formulas=json.dumps(existing_json, ensure_ascii=False) if existing_json else "(none yet - this is the first run)",
        batch=json.dumps(batch_payload, ensure_ascii=False),
    )

    try:
        result = call_ai(settings, prompt, max_tokens=4096)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Validate the AI's response before writing anything: a hallucinated formula_id
    # would otherwise UPDATE 0 rows and then blow up the whole transaction on the
    # matching formula_evidence insert's FK constraint.
    def clean_rating(value, fallback):
        return value if isinstance(value, int) and 1 <= value <= 5 else fallback

    def clean_evidence(ids):
        return [rid for rid in (ids or []) if rid in valid_reel_ids]

    matched = []
    new_items = list(result.get("new") or [])
    for m in (result.get("matched") or []):
        existing_row = existing_by_id.get(m.get("formula_id"))
        if not existing_row or m.get("category") != existing_row["category"]:
            if m.get("category") in category_slugs and m.get("name") and m.get("description"):
                new_items.append(m)  # hallucinated/mismatched id - fall back to treating it as new
            continue
        if m.get("status") not in ("working", "not_working"):
            continue
        matched.append({
            "formula_id": existing_row["id"],
            "rating": clean_rating(m.get("rating"), existing_row.get("rating") or 3),
            "status": m["status"],
            "reason": m.get("reason"),
            "evidence_reel_ids": clean_evidence(m.get("evidence_reel_ids")),
        })

    cleaned_new = []
    for n in new_items:
        if n.get("category") not in category_slugs or not n.get("name") or not n.get("description"):
            continue
        if n.get("status") not in ("working", "not_working"):
            continue
        cleaned_new.append({
            "category": n["category"],
            "name": n["name"].strip(),
            "description": n["description"],
            "reason": n.get("reason"),
            "rating": clean_rating(n.get("rating"), 3),
            "status": n["status"],
            "evidence_reel_ids": clean_evidence(n.get("evidence_reel_ids")),
        })

    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_runs (high_count, low_count, mid_count, summary, caveats) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (high_count, low_count, mid_count, result.get("summary"), result.get("caveats")),
        )
        run_id = cur.fetchone()["id"]

        for m in matched:
            cur.execute(
                "UPDATE formulas SET rating = %s, status = %s, reason = %s, times_seen = times_seen + 1, "
                "updated_at = now(), last_run_id = %s WHERE id = %s",
                (m["rating"], m["status"], m["reason"], run_id, m["formula_id"]),
            )
            for reel_id in m["evidence_reel_ids"]:
                cur.execute(
                    "INSERT INTO formula_evidence (formula_id, reel_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (m["formula_id"], reel_id),
                )

        for n in cleaned_new:
            cur.execute(
                """
                INSERT INTO formulas (category, name, description, reason, rating, status, last_run_id)
                VALUES (%(category)s, %(name)s, %(description)s, %(reason)s, %(rating)s, %(status)s, %(last_run_id)s)
                ON CONFLICT (category, lower(btrim(name))) DO UPDATE SET
                    reason = EXCLUDED.reason, rating = EXCLUDED.rating, status = EXCLUDED.status,
                    times_seen = formulas.times_seen + 1, updated_at = now(), last_run_id = EXCLUDED.last_run_id
                RETURNING id
                """,
                {**n, "last_run_id": run_id},
            )
            new_id = cur.fetchone()["id"]
            for reel_id in n["evidence_reel_ids"]:
                cur.execute(
                    "INSERT INTO formula_evidence (formula_id, reel_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (new_id, reel_id),
                )

    return jsonify({"ok": True, "run_id": run_id})


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
    app.run(host="127.0.0.1", port=5151, debug=False, threaded=True)
