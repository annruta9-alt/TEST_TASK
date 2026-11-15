"""Integration with the Docker stack (skipped automatically when it is not running).

    docker compose up -d --build
    python -m pytest tests/test_stack_integration.py -v

ZOOM_STACK_URL (default http://localhost:8000), ZOOM_TEST_VIDEO (any .webm; default:
a file from ./video or a synthetic video made with a local ffmpeg).
"""
import glob
import json
import os
import shutil
import subprocess
import time

import pytest

requests = pytest.importorskip("requests")
pytestmark = pytest.mark.integration

STACK = os.environ.get("ZOOM_STACK_URL", "http://localhost:8000").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def stack():
    try:
        requests.get(STACK + "/health", timeout=3).raise_for_status()
    except Exception as exc:
        pytest.skip(f"Docker stack is not running at {STACK}: {exc}")
    return STACK


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    explicit = os.environ.get("ZOOM_TEST_VIDEO")
    if explicit and os.path.isfile(explicit):
        return explicit
    found = [p for p in glob.glob(os.path.join(ROOT, "video", "*.webm"))
             if not p.endswith(".zoomed.webm")]
    if found:
        return found[0]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("no .webm to upload: set ZOOM_TEST_VIDEO or install ffmpeg")
    path = str(tmp_path_factory.mktemp("v") / "synthetic.webm")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=1280x720:r=25:d=6", "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p",
                    path], check=True)
    return path


SIDECAR = {"viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0,
           "ramp_sec": 0.5,
           "events": [{"t": 1.0, "x": 640, "y": 360, "type": "in"},
                      {"t": 3.0, "x": 640, "y": 360, "type": "out"}]}


def upload(stack, video, company, sidecar=None):
    files = {"file": (os.path.basename(video), open(video, "rb"))}
    if sidecar is not None:
        files["zoom"] = ("x.zoom.json", json.dumps(sidecar), "application/json")
    r = requests.post(stack + "/upload", files=files, data={"company_name": company}, timeout=120)
    r.raise_for_status()
    return r.json()


def wait_done(stack, company, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = requests.get(stack + "/jobs", params={"company_name": company}, timeout=10).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(2)
    pytest.fail("zoom job did not finish in time")


def test_upload_with_sidecar_is_processed_and_stored_in_postgres(stack, video):
    company = f"pytest-zoom-{int(time.time())}"
    uploaded = upload(stack, video, company, SIDECAR)
    assert uploaded["process_status"] == "pending"

    job = wait_done(stack, company)
    assert job["status"] == "done", job
    assert job["processed_video_url"].endswith(".zoomed.webm")
    assert job["zoom_event_count"] == 2

    # the processed URL is actually playable
    r = requests.get(job["processed_video_url"], headers={"Range": "bytes=0-99"}, timeout=30)
    assert r.status_code in (200, 206) and r.headers["Content-Type"].startswith("video/")

    # and it is what /videos (PostgreSQL) reports
    rows = requests.get(stack + "/videos", timeout=10).json()
    row = next(x for x in rows if x["company_name"] == company)
    assert row["process_status"] == "done" and row["processed_video_url"] == job["processed_video_url"]


def test_upload_without_sidecar_is_not_queued_and_can_be_queued_by_endpoint(stack, video):
    company = f"pytest-plain-{int(time.time())}"
    uploaded = upload(stack, video, company)
    assert uploaded["process_status"] == "none"

    r = requests.post(f"{stack}/jobs/{uploaded['id']}/process", timeout=10)
    assert r.status_code == 202
    job = wait_done(stack, company)
    assert job["status"] == "done"          # no events -> the worker copies the video unchanged


def test_unknown_job_is_404(stack):
    assert requests.post(stack + "/jobs/999999999/process", timeout=10).status_code == 404
    assert requests.get(stack + "/jobs/999999999", timeout=10).status_code == 404


def test_invalid_sidecar_is_rejected(stack, video):
    r = requests.post(stack + "/upload",
                      files={"file": (os.path.basename(video), open(video, "rb")),
                             "zoom": ("x.json", "{broken", "application/json")},
                      data={"company_name": "pytest-bad"}, timeout=60)
    assert r.status_code == 400


def test_reupload_resets_the_processed_result(stack, video):
    company = f"pytest-re-{int(time.time())}"
    upload(stack, video, company, SIDECAR)
    assert wait_done(stack, company)["status"] == "done"
    upload(stack, video, company)                        # new upload without sidecar
    job = requests.get(stack + "/jobs", params={"company_name": company}, timeout=10).json()
    assert job["status"] == "none" and job["processed_video_url"] is None


def _compose(*args):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_ffmpeg_is_installed_in_the_images(stack):
    for service in ("web", "worker"):
        res = _compose("exec", "-T", service, "ffmpeg", "-version")
        if res.returncode != 0:
            pytest.skip(f"cannot exec into '{service}': {res.stderr.strip()[:120]}")
        assert "ffmpeg version" in res.stdout
    enc = _compose("exec", "-T", "worker", "ffmpeg", "-hide_banner", "-encoders")
    assert "libvpx-vp9" in enc.stdout
