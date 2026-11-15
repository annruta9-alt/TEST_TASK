"""PostgreSQL access shared by the web service and the worker."""
import os
import time

import psycopg2
import psycopg2.extras

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,          -- absolute path to the file on disk
    video_url TEXT NOT NULL,          -- public URL to the video
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Added for the zoom step. "IF NOT EXISTS" makes this safe on a database that was
# created by the previous task (the db_data volume keeps the old table).
MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS zoom_events JSONB",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_file_path TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_video_url TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS process_error TEXT",
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ",
]
# process_status: none -> (pending -> processing -> done | failed)


def connect():
    """New connection (psycopg2 connections must not be shared between threads)."""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def init_db(wait_seconds=60):
    """Create/upgrade the table. Waits for the database instead of crashing."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    deadline = time.time() + wait_seconds
    while True:
        try:
            conn = connect()
            break
        except psycopg2.OperationalError as exc:
            if time.time() > deadline:
                raise
            print(f"waiting for database: {str(exc).strip()}", flush=True)
            time.sleep(2)
    try:
        with conn, conn.cursor() as cur:
            # advisory lock: several processes (Apache workers, the zoom worker)
            # may start at the same moment
            cur.execute("SELECT pg_advisory_xact_lock(827364)")
            cur.execute(SCHEMA)
            for statement in MIGRATIONS:
                cur.execute(statement)
    finally:
        conn.close()
