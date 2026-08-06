"""One-time migration of data/store.json + data/settings.json into Neon.

Idempotent (ON CONFLICT DO UPDATE) - safe to re-run. Assumes schema.sql has
already been applied. Run once: venv\\Scripts\\python.exe migrate_to_neon.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_SETTINGS = {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": ""}


def parse_created_at(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main():
    database_url = os.environ["DATABASE_URL"]

    store_path = DATA_DIR / "store.json"
    settings_path = DATA_DIR / "settings.json"

    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    settings = {**DEFAULT_SETTINGS, **(json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {})}

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                reel_count = 0
                for rid, job in store.items():
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
                        {
                            "id": rid,
                            "url": job.get("url"),
                            "status": job.get("status"),
                            "transcript": job.get("transcript"),
                            "segments": psycopg2.extras.Json(job.get("segments")),
                            "video_file": job.get("video_file"),
                            "thumb_file": job.get("thumb_file"),
                            "created_at": parse_created_at(job.get("created_at")),
                            "meta": psycopg2.extras.Json(job.get("meta")),
                            "analysis": psycopg2.extras.Json(job.get("analysis")),
                            "error": job.get("error"),
                        },
                    )
                    reel_count += 1

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
        print(f"Migrated {reel_count} reel(s) and settings into Neon.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
