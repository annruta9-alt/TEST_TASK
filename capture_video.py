"""Reads input.csv and records a browser-automation video for every row."""
import csv
import os
import sys

import qscript

CSV_FILE = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
VIDEO_DIR = os.path.abspath("./video")


def main():
    qscript.PATH_TO_VIDEO_FOLDER = VIDEO_DIR  # override the module default
    os.makedirs(VIDEO_DIR, exist_ok=True)

    with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for i, row in enumerate(reader, 1):
            print(f"[{i}] {row['Company Name']} ...", flush=True)
            path = qscript.CaptureVideo_Magento(
                row["URL"],
                row["Company Name"],
                row["Language"],
                row["Country"],
                row["Postcode"],
                row["City"],
                row["Street"],
                row["HouseNo"],
            )
            print(f"[{i}] saved: {path}" if path else f"[{i}] FAILED", flush=True)


if __name__ == "__main__":
    main()
