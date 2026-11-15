"""Zoom worker: picks up pending videos from PostgreSQL and runs apply_zoom on them.

Runs in its own container (same image as the web service, ffmpeg inside):
    python worker.py          # loop forever
    python worker.py --once   # process what is pending, then exit (used by tests)
"""
import argparse
import os
import sys
import time
from urllib.parse import quote

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_zoom  # noqa: E402
import db  # noqa: E402

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


def claim_job(conn):
    """Atomically take the oldest pending job (safe with several workers)."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos SET process_status = 'processing', process_error = NULL
            WHERE id = (SELECT id FROM videos WHERE process_status = 'pending'
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, company_name, file_path, video_url
            """
        )
        return cur.fetchone()


def finish_job(conn, job_id, set_now=False, **fields):
    """Store the result; skipped when the video was re-uploaded meanwhile."""
    sets = [f"{k} = %s" for k in fields]
    if set_now:
        sets.append("processed_at = NOW()")
    with conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE videos SET {', '.join(sets)} WHERE id = %s AND process_status = 'processing'",
            (*fields.values(), job_id),
        )


def run_job(conn, job):
    video = job["file_path"]
    stem = os.path.splitext(video)[0]
    sidecar = stem + ".zoom.json"
    output = stem + ".zoomed.webm"
    print(f"[job {job['id']}] {job['company_name']}: processing {os.path.basename(video)}",
          flush=True)
    try:
        if not os.path.isfile(video):
            raise FileNotFoundError(f"video file is missing: {video}")
        result = apply_zoom.process_video(
            video, sidecar if os.path.isfile(sidecar) else None, output, ffmpeg=FFMPEG)
        url = job["video_url"].rsplit("/", 1)[0] + "/" + quote(os.path.basename(output))
        finish_job(conn, job["id"], set_now=True, process_status="done",
                   processed_file_path=output, processed_video_url=url, process_error=None)
        note = (f"{result['segments']} zoom(s) rendered" if result["mode"] == "zoomed"
                else "NO zoom events found - video copied unchanged")
        print(f"[job {job['id']}] done: {note}: {url}", flush=True)
    except Exception as exc:  # keep the worker alive whatever happens to one job
        finish_job(conn, job["id"], process_status="failed", process_error=str(exc)[:1000])
        print(f"[job {job['id']}] FAILED: {exc}", flush=True)


def recover_stuck_jobs():
    """After a crash/restart a job can be left 'processing': queue it again."""
    conn = db.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE videos SET process_status = 'pending' "
                        "WHERE process_status = 'processing'")
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="process pending jobs and exit")
    args = ap.parse_args(argv)

    db.init_db()
    recover_stuck_jobs()
    print("zoom worker started", flush=True)
    while True:
        job = None
        conn = None
        try:
            conn = db.connect()
            job = claim_job(conn)
            if job:
                run_job(conn, job)
        except psycopg2.Error as exc:
            print(f"database error: {str(exc).strip()}", flush=True)
            time.sleep(POLL_SECONDS)
        finally:
            if conn is not None:
                conn.close()
        if job is None:
            if args.once:
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())