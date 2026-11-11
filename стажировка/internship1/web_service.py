"""Video web service (Flask + PostgreSQL).

Endpoints: POST /upload, GET /get_video_url, GET /media/<file>, GET /videos, GET /health

DB settings come from environment variables (see docker-compose.yml):
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
In Docker the service runs under Apache + mod_wsgi (see app.wsgi);
for local debugging:  python web_service.py
"""
import hashlib
import os
import time
import uuid

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
ALLOWED_EXT = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}

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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB


# ----------------------------------------------------------------------- database
def _connect():
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
    """Create the table. Waits for the database instead of crashing if it is not ready."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    deadline = time.time() + wait_seconds
    while True:
        try:
            conn = _connect()
            break
        except psycopg2.OperationalError as exc:
            if time.time() > deadline:
                raise
            print(f"waiting for database: {str(exc).strip()}", flush=True)
            time.sleep(2)
    try:
        with conn, conn.cursor() as cur:
            # advisory lock: several Apache worker processes may start at once
            cur.execute("SELECT pg_advisory_xact_lock(827364)")
            cur.execute(SCHEMA)
    finally:
        conn.close()


def get_db():
    """One connection per request, closed in teardown."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ----------------------------------------------------------------------- helpers
def _make_filename(cur, company_name, original_name):
    """<safe company name><ext>; add a short hash if another company already owns it."""
    ext = os.path.splitext(original_name or "")[1].lower() or ".webm"
    digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
    base = secure_filename(company_name) or digest  # non-ASCII names may sanitize to ""
    filename = base + ext
    cur.execute(
        "SELECT company_name FROM videos WHERE lower(file_path) = lower(%s)",
        (os.path.join(UPLOAD_DIR, filename),),
    )
    row = cur.fetchone()
    if row and row["company_name"] != company_name:
        filename = f"{base}_{digest}{ext}"
    return filename


def _replace(src, dst, attempts=10):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.3)


# ----------------------------------------------------------------------- endpoints
@app.post("/upload")
def upload_video():
    file = request.files.get("file")
    company_name = (request.form.get("company_name") or "").strip()
    if file is None or not file.filename:
        return jsonify(error="field 'file' is required"), 400
    if not company_name:
        return jsonify(error="field 'company_name' is required"), 400
    ext = os.path.splitext(file.filename)[1].lower() or ".webm"
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"unsupported file type '{ext}'"), 400

    db = get_db()
    with db.cursor() as cur:
        filename = _make_filename(cur, company_name, file.filename)
    dest = os.path.join(UPLOAD_DIR, filename)
    tmp = os.path.join(UPLOAD_DIR, f".{uuid.uuid4().hex}.part")  # unique per request
    try:
        file.save(tmp)
        _replace(tmp, dest)  # atomic: readers never see a half-written file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    video_url = url_for("media", filename=filename, _external=True)  # from request host
    with db, db.cursor() as cur:  # `with db` commits the transaction
        cur.execute(
            """
            INSERT INTO videos (company_name, file_path, video_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (company_name) DO UPDATE SET
                file_path = EXCLUDED.file_path,
                video_url = EXCLUDED.video_url,
                updated_at = NOW()
            """,
            (company_name, dest, video_url),
        )
    return jsonify(company_name=company_name, video_url=video_url), 200


@app.get("/get_video_url")
def get_video_url():
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return jsonify(error="query parameter 'company_name' is required"), 400
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT company_name, video_url FROM videos WHERE company_name = %s",
            (company_name,),
        )
        row = cur.fetchone()
    if row is None:
        return jsonify(error="company not found", company_name=company_name), 404
    return jsonify(company_name=row["company_name"], video_url=row["video_url"]), 200


@app.get("/media/<path:filename>")
def media(filename):
    # In Docker Apache serves /media directly (Alias); this route is the fallback
    # and also builds the URL via url_for.
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.get("/videos")
def list_videos():
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT id, company_name, file_path, video_url, created_at, updated_at "
            "FROM videos ORDER BY id"
        )
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        r["updated_at"] = r["updated_at"].isoformat()
    return jsonify(rows), 200


@app.get("/health")
def health():
    with get_db().cursor() as cur:
        cur.execute("SELECT 1")
    return jsonify(status="ok"), 200


init_db()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8000")),
        threaded=True,
    )
