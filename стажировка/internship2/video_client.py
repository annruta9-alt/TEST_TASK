"""Client functions for the video web service."""
import os
import time

import requests


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL.

    If a zoom sidecar (<video stem>.zoom.json) lies next to the video it is uploaded
    too; the server then queues the video for zoom post-processing automatically.
    """
    url = server_url.rstrip("/") + "/upload"
    sidecar = os.path.splitext(video_path)[0] + ".zoom.json"
    with open(video_path, "rb") as fh:
        files = {"file": (os.path.basename(video_path), fh)}
        zoom_fh = None
        try:
            if os.path.isfile(sidecar):
                zoom_fh = open(sidecar, "rb")
                files["zoom"] = (os.path.basename(sidecar), zoom_fh, "application/json")
            resp = requests.post(url, files=files,
                                 data={"company_name": company_name}, timeout=300)
        finally:
            if zoom_fh:
                zoom_fh.close()
    resp.raise_for_status()
    return resp.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    resp = requests.get(
        server_url.rstrip("/") + "/get_video_url",
        params={"company_name": company_name},
        timeout=30,
    )
    resp.raise_for_status()  # 404 -> requests.HTTPError for unknown names
    return resp.json()["video_url"]


def DownloadFile(url: str, dest_path: str) -> str:
    """Saves the file behind `url` to `dest_path` (used for the zoomed video)."""
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        tmp = dest_path + ".part"
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                fh.write(chunk)
    os.replace(tmp, dest_path)
    return dest_path


def GetJob(server_url: str, company_name: str) -> dict:
    """Zoom-processing status of a company's video (status, processed_video_url, ...)."""
    resp = requests.get(server_url.rstrip("/") + "/jobs",
                        params={"company_name": company_name}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def WaitForProcessed(server_url: str, company_name: str, timeout: float = 300,
                     poll: float = 2.0) -> str:
    """Waits until the zoom step is done and returns the processed video URL.

    Raises RuntimeError if the job failed, TimeoutError if it takes longer than `timeout`.
    """
    deadline = time.time() + timeout
    while True:
        job = GetJob(server_url, company_name)
        if job["status"] == "done" and job.get("processed_video_url"):
            return job["processed_video_url"]
        if job["status"] == "failed":
            raise RuntimeError(f"zoom processing failed: {job.get('error')}")
        if job["status"] == "none":
            raise RuntimeError("video is not queued for processing (no zoom sidecar uploaded)")
        if time.time() > deadline:
            raise TimeoutError(f"zoom processing not finished after {timeout:.0f}s "
                               f"(status: {job['status']})")
        time.sleep(poll)