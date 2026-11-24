"""PostgreSQL access shared by the web service and the enrichment script."""
import os
import time

import psycopg2
import psycopg2.extras

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
SCHEMA_FILE = os.path.join(BASE_DIR, "schema.sql")


def connect():
    """New connection (psycopg2 connections must not be shared between threads).
    Settings come from the environment, nothing is hard-coded."""
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
    """Run schema.sql (idempotent). Waits for the database instead of crashing."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(SCHEMA_FILE, encoding="utf-8") as fh:
        schema = fh.read()
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
            # advisory lock: several Apache worker processes start at the same moment
            cur.execute("SELECT pg_advisory_xact_lock(827364)")
            cur.execute(schema)
    finally:
        conn.close()
