"""Video web service: upload, lookup by company name, serve files.

Run:  python web_service.py
Env:  FLASK_HOST (default 0.0.0.0), FLASK_PORT (default 8000)
"""
import hashlib
import os
import sqlite3
import time
import uuid

from flask import Flask, g, jsonify, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "videos.db")
ALLOWED_EXT = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    video_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB


# ----------------------------------------------------------------------- database
def _connect():
    # timeout = SQLite busy timeout (seconds): concurrent writers wait, not fail
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """One connection per request (SQLite connections must not be shared across threads)."""
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ----------------------------------------------------------------------- helpers
def _make_filename(db, company_name, original_name):
    """<safe company name><ext>; add a short hash if another company already owns it."""
    ext = os.path.splitext(original_name or "")[1].lower() or ".webm"
    digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:8]
    base = secure_filename(company_name) or digest  # non-ASCII names may sanitize to ""
    filename = base + ext
    # compare case-insensitively: Windows/macOS file systems treat A.webm == a.webm
    row = db.execute(
        "SELECT company_name FROM videos WHERE lower(file_path) = lower(?)",
        (os.path.join(UPLOAD_DIR, filename),),
    ).fetchone()
    if row and row["company_name"] != company_name:
        filename = f"{base}_{digest}{ext}"
    return filename


def _replace(src, dst, attempts=10):
    """os.replace with retries: on Windows the target may be briefly locked
    while a browser is streaming the previous version of the video."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.3)


def _payload(row):
    return {"company_name": row["company_name"], "video_url": row["video_url"]}


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
    filename = _make_filename(db, company_name, file.filename)
    dest = os.path.join(UPLOAD_DIR, filename)
    tmp = os.path.join(UPLOAD_DIR, f".{uuid.uuid4().hex}.part")  # unique per request
    try:
        file.save(tmp)
        _replace(tmp, dest)  # atomic: readers never see a half-written file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    video_url = url_for("media", filename=filename, _external=True)  # from request host
    db.execute(
        """
        INSERT INTO videos (company_name, file_path, video_url)
        VALUES (?, ?, ?)
        ON CONFLICT(company_name) DO UPDATE SET
            file_path = excluded.file_path,
            video_url = excluded.video_url,
            updated_at = datetime('now')
        """,
        (company_name, dest, video_url),
    )
    db.commit()
    return jsonify(company_name=company_name, video_url=video_url), 200


@app.get("/get_video_url")
def get_video_url():
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return jsonify(error="query parameter 'company_name' is required"), 400
    row = get_db().execute(
        "SELECT company_name, video_url FROM videos WHERE company_name = ?",
        (company_name,),
    ).fetchone()
    if row is None:
        return jsonify(error="company not found", company_name=company_name), 404
    return jsonify(_payload(row)), 200


@app.get("/media/<path:filename>")
def media(filename):
    # send_from_directory blocks path traversal and supports Range requests (seeking)
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.get("/videos")
def list_videos():
    rows = get_db().execute(
        "SELECT id, company_name, file_path, video_url, created_at, updated_at "
        "FROM videos ORDER BY id"
    ).fetchall()
    return jsonify([dict(r) for r in rows]), 200


init_db()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8000")),
        threaded=True,
    )