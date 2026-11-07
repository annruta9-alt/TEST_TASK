"""Reads the CSV, records a video per row; optionally uploads and previews it."""
import argparse
import csv
import os

import qscript

VIDEO_DIR = os.path.abspath("./video")


def preview(url, view_seconds):
    """Open the uploaded video in a visible Chromium window."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)  # Chromium's built-in media player
            if view_seconds > 0:
                page.wait_for_timeout(int(view_seconds * 1000))
            else:
                try:
                    input("Preview is open. Press Enter to close the browser... ")
                except EOFError:
                    pass
        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_file", nargs="?", default="input.csv")
    ap.add_argument("--server", help="web service URL, e.g. http://127.0.0.1:8000; "
                                     "enables upload + preview")
    ap.add_argument("--view-seconds", type=float, default=10,
                    help="seconds to keep the preview open; 0 = until Enter (default 10)")
    args = ap.parse_args()

    qscript.PATH_TO_VIDEO_FOLDER = VIDEO_DIR  # override the module default
    os.makedirs(VIDEO_DIR, exist_ok=True)

    upload = None
    if args.server:
        from video_client import UploadVideo as upload  # lazy: requests only if needed

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for i, row in enumerate(reader, 1):
            name = row["Company Name"]
            print(f"[{i}] {name} ...", flush=True)
            path = qscript.CaptureVideo_Magento(
                row["URL"], name, row["Language"], row["Country"],
                row["Postcode"], row["City"], row["Street"], row["HouseNo"],
            )
            if not path:
                print(f"[{i}] FAILED", flush=True)
                continue
            print(f"[{i}] saved: {path}", flush=True)

            if not upload:
                continue
            try:
                video_url = upload(args.server, path, name)
                print(f"[{i}] uploaded: {video_url}", flush=True)
            except Exception as exc:  # never stop the batch
                print(f"[{i}] upload failed: {exc!r}", flush=True)
                continue
            try:
                preview(video_url, args.view_seconds)
            except Exception as exc:
                print(f"[{i}] preview failed: {exc!r}", flush=True)


if __name__ == "__main__":
    main()