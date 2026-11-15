"""Reads the CSV, records a video per row; optionally uploads, zooms and previews it."""
import argparse
import csv
import json
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


def describe_zoom(sidecar_path):
    """One line telling how many zooms were recorded (and whether the last one is open)."""
    try:
        with open(sidecar_path, encoding="utf-8") as fh:
            events = json.load(fh)["events"]
    except Exception:
        return "zoom sidecar could not be read"
    ins = sum(1 for e in events if e["type"] == "in")
    outs = sum(1 for e in events if e["type"] == "out")
    text = f"zoom recorded: {ins} zoom-in, {outs} zoom-out"
    if ins > outs:
        text += " (the last zoom stays on until the end of the video)"
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_file", nargs="?", default="input.csv")
    ap.add_argument("--server", help="web service URL, e.g. http://127.0.0.1:8000; "
                                     "enables upload + zoom + preview")
    ap.add_argument("--view-seconds", type=float, default=10,
                    help="seconds to keep the preview open; 0 = until Enter (default 10)")
    ap.add_argument("--no-zoom", action="store_true",
                    help="the sample scenario does not trigger any zoom by itself; "
                         "use this to zoom by hand with F5 (do not press F5 during the "
                         "automatic demo: it would flip the in/out state)")
    ap.add_argument("--process-timeout", type=float, default=300,
                    help="seconds to wait for the zoom step on the server (default 300)")
    args = ap.parse_args()

    qscript.PATH_TO_VIDEO_FOLDER = VIDEO_DIR  # override the module default
    if args.no_zoom:
        qscript.ZOOM_DEMO = False
    os.makedirs(VIDEO_DIR, exist_ok=True)

    upload = wait_processed = download = None
    if args.server:
        # lazy import: `requests` is only needed when --server is used
        from video_client import (UploadVideo as upload, WaitForProcessed as wait_processed,
                                  DownloadFile as download)

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
            sidecar = os.path.splitext(path)[0] + ".zoom.json"
            if os.path.isfile(sidecar):
                print(f"[{i}] {describe_zoom(sidecar)}", flush=True)
            elif args.server:
                print(f"[{i}] no zoom events were recorded: the server will not zoom this "
                      f"video (press F5 during the recording, or run without --no-zoom)",
                      flush=True)

            if not upload:
                continue
            try:
                video_url = upload(args.server, path, name)
                print(f"[{i}] uploaded: {video_url}", flush=True)
            except Exception as exc:  # never stop the batch
                print(f"[{i}] upload failed: {exc!r}", flush=True)
                continue

            preview_url = video_url
            if os.path.isfile(os.path.splitext(path)[0] + ".zoom.json"):
                # zoom events were recorded: the server (worker container) renders them
                try:
                    print(f"[{i}] waiting for the zoom step on the server ...", flush=True)
                    preview_url = wait_processed(args.server, name, args.process_timeout)
                    print(f"[{i}] zoomed: {preview_url}", flush=True)
                    local = os.path.splitext(path)[0] + ".zoomed.webm"
                    download(preview_url, local)
                    print(f"[{i}] zoomed video saved: {local}", flush=True)
                except Exception as exc:
                    print(f"[{i}] zoom step failed, previewing the original: {exc!r}",
                          flush=True)
            try:
                preview(preview_url, args.view_seconds)
            except Exception as exc:
                print(f"[{i}] preview failed: {exc!r}", flush=True)


if __name__ == "__main__":
    main()