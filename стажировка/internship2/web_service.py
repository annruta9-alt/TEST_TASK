"""Video web service (Flask + PostgreSQL) with a zoom-processing queue.

Endpoints
  POST /upload                    video (+ optional zoom sidecar), upsert by company
  GET  /get_video_url             original video URL by company name
  GET  /media/<file>              files (Apache serves /media/ directly in Docker)
  GET  /videos                    all records
  POST /jobs/<id>/process         queue the zoom post-processing (picked up by the worker)
  GET  /jobs/<id>                 job status + processed_video_url
  GET  /jobs?company_name=...     same, looked up by company name
  GET  /health

DB settings come from environment variables: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
"""
import hashlib
import json
import os
import time
import uuid

import psycopg2.extras
from flask import Flask, g, jsonify, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

import apply_zoom
import db
from db import UPLOAD_DIR

ALLOWED_EXT = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}
MAX_SIDECAR_BYTES = 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB


# ----------------------------------------------------------------------- database
def get_db():
    """One connection per request, closed in teardown."""
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


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


def _save_atomic(write, dest):
    tmp = os.path.join(UPLOAD_DIR, f".{uuid.uuid4().hex}.part")  # unique per request
    try:
        write(tmp)
        _replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _remove_quietly(path):
    """Delete a file we own (inside UPLOAD_DIR only)."""
    try:
        if path and os.path.abspath(path).startswith(os.path.abspath(UPLOAD_DIR) + os.sep):
            os.remove(path)
    except OSError:
        pass


def _job_payload(row):
    return {
        "id": row["id"],
        "company_name": row["company_name"],
        "status": row["process_status"],
        "video_url": row["video_url"],
        "processed_video_url": row["processed_video_url"],
        "error": row["process_error"],
        "zoom_event_count": len((row["zoom_events"] or {}).get("events", [])),
        "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
    }


JOB_COLUMNS = ("id, company_name, video_url, process_status, processed_video_url, "
               "process_error, zoom_events, processed_at")


# ----------------------------------------------------------------------- endpoints
@app.post("/upload")
def upload_video():
    file = request.files.get("file")
    zoom_file = request.files.get("zoom")  # optional <video>.zoom.json sidecar
    company_name = (request.form.get("company_name") or "").strip()
    if file is None or not file.filename:
        return jsonify(error="field 'file' is required"), 400
    if not company_name:
        return jsonify(error="field 'company_name' is required"), 400
    ext = os.path.splitext(file.filename)[1].lower() or ".webm"
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"unsupported file type '{ext}'"), 400

    zoom_data = None
    if zoom_file is not None and zoom_file.filename:
        raw = zoom_file.read(MAX_SIDECAR_BYTES + 1)
        if len(raw) > MAX_SIDECAR_BYTES:
            return jsonify(error="zoom sidecar is too large"), 400
        try:
            zoom_data = json.loads(raw.decode("utf-8-sig"))
            apply_zoom.parse_sidecar(zoom_data)  # validate only; stored as sent
        except (ValueError, UnicodeDecodeError) as exc:
            return jsonify(error=f"invalid zoom sidecar: {exc}"), 400
        if isinstance(zoom_data, list):
            zoom_data = {"events": zoom_data}

    wants_processing = zoom_data is not None or \
        (request.form.get("process") or "").lower() in ("1", "true", "yes")

    conn = get_db()
    with conn.cursor() as cur:
        filename = _make_filename(cur, company_name, file.filename)
        cur.execute("SELECT processed_file_path FROM videos WHERE company_name = %s",
                    (company_name,))
        old = cur.fetchone()
    dest = os.path.join(UPLOAD_DIR, filename)
    stem = os.path.splitext(dest)[0]
    sidecar_path = stem + ".zoom.json"

    _save_atomic(file.save, dest)  # atomic: readers never see a half-written file
    if zoom_data is not None:
        _save_atomic(lambda p: open(p, "w", encoding="utf-8").write(
            json.dumps(zoom_data, indent=2)), sidecar_path)
    else:
        _remove_quietly(sidecar_path)  # a stale sidecar must not be applied to a new video

    video_url = url_for("media", filename=filename, _external=True)  # from request host
    status = "pending" if wants_processing else "none"
    with conn, conn.cursor() as cur:  # `with conn` commits the transaction
        cur.execute(
            """
            INSERT INTO videos (company_name, file_path, video_url, zoom_events, process_status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (company_name) DO UPDATE SET
                file_path = EXCLUDED.file_path,
                video_url = EXCLUDED.video_url,
                zoom_events = EXCLUDED.zoom_events,
                process_status = EXCLUDED.process_status,
                processed_file_path = NULL,
                processed_video_url = NULL,
                process_error = NULL,
                processed_at = NULL,
                updated_at = NOW()
            RETURNING id
            """,
            (company_name, dest, video_url,
             psycopg2.extras.Json(zoom_data) if zoom_data is not None else None, status),
        )
        job_id = cur.fetchone()["id"]
    if old and old["processed_file_path"]:
        _remove_quietly(old["processed_file_path"])  # result of the previous upload
    return jsonify(id=job_id, company_name=company_name, video_url=video_url,
                   process_status=status), 200


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


@app.post("/jobs/<int:job_id>/process")
def process_job(job_id):
    """Queue the zoom step for this video; the worker container picks it up."""
    conn = get_db()
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos SET process_status = 'pending', process_error = NULL,
                   processed_file_path = NULL, processed_video_url = NULL, processed_at = NULL
            WHERE id = %s AND process_status <> 'processing'
            RETURNING id
            """,
            (job_id,),
        )
        queued = cur.fetchone()
        if queued is None:
            cur.execute("SELECT 1 FROM videos WHERE id = %s", (job_id,))
            exists = cur.fetchone()
    if queued:
        return jsonify(id=job_id, status="pending"), 202
    if exists:
        return jsonify(error="job is already being processed", id=job_id), 409
    return jsonify(error="job not found", id=job_id), 404


@app.get("/jobs/<int:job_id>")
def get_job(job_id):
    with get_db().cursor() as cur:
        cur.execute(f"SELECT {JOB_COLUMNS} FROM videos WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        return jsonify(error="job not found", id=job_id), 404
    return jsonify(_job_payload(row)), 200


@app.get("/jobs")
def find_job():
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return jsonify(error="query parameter 'company_name' is required"), 400
    with get_db().cursor() as cur:
        cur.execute(f"SELECT {JOB_COLUMNS} FROM videos WHERE company_name = %s",
                    (company_name,))
        row = cur.fetchone()
    if row is None:
        return jsonify(error="company not found", company_name=company_name), 404
    return jsonify(_job_payload(row)), 200


@app.get("/media/<path:filename>")
def media(filename):
    # In Docker Apache serves /media directly (Alias); this route is the fallback
    # and also builds the URL via url_for.
    return send_from_directory(UPLOAD_DIR, filename, conditional=True)


@app.get("/videos")
def list_videos():
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT id, company_name, file_path, video_url, process_status, "
            "processed_video_url, created_at, updated_at FROM videos ORDER BY id"
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


db.init_db()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8000")),
        threaded=True,
    )
