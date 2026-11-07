"""Client functions for the video web service."""
import os

import requests


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    url = server_url.rstrip("/") + "/upload"
    with open(video_path, "rb") as fh:
        resp = requests.post(
            url,
            files={"file": (os.path.basename(video_path), fh)},
            data={"company_name": company_name},
            timeout=300,
        )
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