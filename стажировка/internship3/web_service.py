"""Web service (Flask + PostgreSQL): videos (Task 1) and companies (Task 3).

Videos     POST /upload, GET /get_video_url, GET /media/<file>, GET /videos
Companies  GET/POST /upload_companies (CSV import form), GET /companies[?status=N],
           GET /companies/<company_name>
Other      GET /health

DB settings come from environment variables: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
In Docker the service runs under Apache + mod_wsgi (see app.wsgi);
for local debugging:  python web_service.py
"""
import csv
import hashlib
import io
import os
import re
import time
import uuid

from flask import (Flask, g, jsonify, render_template_string, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import psycopg2

import db
from db import UPLOAD_DIR
from status import Status, status_name

ALLOWED_EXT = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB


# ----------------------------------------------------------------------- database
def get_db():
    """One connection per request, closed in teardown."""
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.errorhandler(psycopg2.Error)
def handle_database_error(exc):
    app.logger.exception("database error")
    return jsonify(error="database error"), 500


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

    conn = get_db()
    with conn.cursor() as cur:
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
    with conn, conn.cursor() as cur:  # `with conn` commits the transaction
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


# ======================================================================= companies
# CSV header (normalised: lower case, no spaces/underscores) -> column of `companies`.
# The "URL" column belongs to the recorder and is ignored here.
CSV_COLUMNS = {
    "companyname": "company_name", "language": "language", "country": "country",
    "postcode": "postcode", "city": "city", "street": "street",
    "houseno": "house_no", "web": "web",
}
CSV_DB_COLUMNS = ["company_name", "language", "country", "postcode", "city",
                  "street", "house_no", "web"]

# only the CSV columns are updated on re-import: status and the extracted fields stay
UPSERT_COMPANY = """
INSERT INTO companies (company_name, language, country, postcode, city, street, house_no, web)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (company_name) DO UPDATE SET
    language = EXCLUDED.language, country = EXCLUDED.country,
    postcode = EXCLUDED.postcode, city = EXCLUDED.city, street = EXCLUDED.street,
    house_no = EXCLUDED.house_no, web = EXCLUDED.web, updated_at = NOW()
RETURNING (xmax = 0) AS inserted
"""

COMPANY_COLUMNS = (
    "id, company_name, language, country, postcode, city, street, house_no, web, status, "
    "impressum_url, legal_name, postal_address, handelsregister, vat_number, phone, email, "
    "responsible_person, raw_json, created_at, updated_at")

UPLOAD_FORM = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Upload companies</title></head>
<body style="font-family: sans-serif; max-width: 42rem; margin: 2rem auto;">
  <h1>Upload companies (CSV)</h1>
  <p>UTF-8, delimiter <code>;</code>, quote <code>"</code>. Header:<br>
     <code>URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web</code><br>
     Required: <code>Company Name</code> (unique key) and <code>Web</code>.
     Re-uploading updates existing companies and keeps their enrichment results.</p>
  <form method="post" action="/upload_companies" enctype="multipart/form-data">
    <input type="file" name="file" accept=".csv,text/csv" required>
    <button type="submit">Upload</button>
  </form>
  <p><a href="/companies">View all companies (JSON)</a></p>
</body>
</html>
"""


def _norm_header(name):
    return re.sub(r"[\s_\-]+", "", (name or "").strip().lower())


def _cell(value):
    value = (value or "").strip()
    return value or None


def _company_json(row):
    row = dict(row)
    row["status_name"] = status_name(row["status"])
    for key in ("created_at", "updated_at"):
        row[key] = row[key].isoformat()
    return row


@app.get("/upload_companies")
def upload_companies_form():
    return render_template_string(UPLOAD_FORM)


@app.post("/upload_companies")
def upload_companies():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(error="field 'file' (the CSV) is required"), 400
    try:
        text = file.read().decode("utf-8-sig")  # UTF-8, BOM tolerated
    except UnicodeDecodeError:
        return jsonify(error="the CSV must be UTF-8 encoded"), 400
    if "\x00" in text:
        return jsonify(error="the file contains NUL bytes: it is not a text CSV"), 400

    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";", quotechar='"')
    mapping = {}
    for header in reader.fieldnames or []:
        column = CSV_COLUMNS.get(_norm_header(header))
        if column:
            mapping[header] = column
    if "company_name" not in mapping.values():
        return jsonify(error="column 'Company Name' not found; the CSV must use ';' as "
                             "delimiter and start with a header row",
                       header=reader.fieldnames), 400

    imported = updated = skipped = total = 0
    conn = get_db()
    with conn, conn.cursor() as cur:  # one transaction: the import is all-or-nothing
        for row in reader:
            total += 1
            values = {column: _cell(row.get(header)) for header, column in mapping.items()}
            if not values.get("company_name"):
                skipped += 1  # a row without a company name cannot be stored
                continue
            cur.execute(UPSERT_COMPANY, [values.get(c) for c in CSV_DB_COLUMNS])
            if cur.fetchone()["inserted"]:
                imported += 1
            else:
                updated += 1
    return jsonify(imported=imported, updated=updated, total_rows=total, skipped=skipped), 200


@app.get("/companies")
def list_companies():
    raw = (request.args.get("status") or "").strip().lower()
    sql, params = f"SELECT {COMPANY_COLUMNS} FROM companies", []
    if raw:
        if raw in ("null", "none", "pending"):
            sql += " WHERE status IS NULL"
        else:
            try:
                value = int(raw)
            except ValueError:
                return jsonify(error="status must be an integer code or 'pending'"), 400
            sql += " WHERE status = %s"
            params.append(value)
    with get_db().cursor() as cur:
        cur.execute(sql + " ORDER BY id", params)
        rows = cur.fetchall()
    return jsonify([_company_json(r) for r in rows]), 200


@app.get("/companies/<path:company_name>")
def get_company(company_name):
    with get_db().cursor() as cur:
        cur.execute(f"SELECT {COMPANY_COLUMNS} FROM companies WHERE company_name = %s",
                    (company_name,))
        row = cur.fetchone()
    if row is None:
        return jsonify(error="company not found", company_name=company_name), 404
    return jsonify(_company_json(row)), 200


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
