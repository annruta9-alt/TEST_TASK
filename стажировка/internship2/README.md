# Camtasia-style zoom for recorded videos (Internship Task 2)

The recorder (Playwright) writes **zoom events** next to every video; the Docker stack from
Internship Task 1 renders them with **ffmpeg** (smooth zoom-in -> hold -> zoom-out) and stores
the result in PostgreSQL.

```
 host                                   Docker (docker compose)
 ┌──────────────────────────┐           ┌───────────────────────────────────────────┐
 │ capture_video.py         │  upload   │ web     Apache + mod_wsgi + Flask  :8000  │
 │  └ qscript.py            │ ────────► │ worker  python worker.py + ffmpeg         │
 │    video + <video>.zoom.json         │ db      PostgreSQL 16                     │
 │                          │ ◄──────── │ volumes: db_data, uploads                 │
 └──────────────────────────┘  zoomed   └───────────────────────────────────────────┘
```

## Run the stack

```
docker compose up -d --build
```

Stop the stack of the previous task first (`docker compose down` in its folder): it uses port 8000.
`docker compose ps` should show `db`, `web` (healthy) and `worker` running.
The existing `videos` table is upgraded automatically (`ALTER TABLE ... IF NOT EXISTS`).

## Record with zoom

```
python -m pip install -r requirements-recorder.txt
python -m playwright install chromium
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10
```

* The sample scenario triggers the zoom itself with `window.__zoomAt(x, y)` (search box while
  typing, then the first result). `--no-zoom` disables that.
* **F5** toggles the zoom by hand while the browser window is focused: first press = zoom-in
  starts at the pointer, second press = zoom-out starts. A coloured ring shows the state
  (green = in, red = out). F5 does not reload the page.
* Output: `video/<Company>.webm` and, only if at least one event exists, `video/<Company>.zoom.json`.
* With `--server` the video and the sidecar are uploaded, the worker renders the zoom, and the
  recorder previews the **zoomed** video in Chromium.

## `apply_zoom.py` (also runs on the host, needs ffmpeg)

```
python apply_zoom.py --video video/ACME_GmbH.webm              # -> video/ACME_GmbH.zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run    # print the ffmpeg command only
python apply_zoom.py --video x.webm --ffmpeg C:\tools\ffmpeg\bin\ffmpeg.exe
```
Other options: `--sidecar`, `--output`, `--zoom-factor`, `--ramp-sec`, `--time-offset`, `--no-sync`.

* Every `in` is paired with the next `out`: ease-in ramp -> hold (the time between the two
  presses) -> ease-out ramp. Several non-overlapping pairs are supported. An `in` without an
  `out` holds the zoom to the end of the video.
* The crop is centred on the mouse point and clamped to the frame:
  `x = clamp(px - iw/(2*zoom), 0, iw - iw/zoom)` (same for y). Mouse and video pixels are 1:1
  because `record_video_size == viewport` (scaled automatically if a video has another size).
* Output: VP9 `.webm`, same resolution. Without events the video is copied unchanged.

### Sidecar `<video>.zoom.json`

```json
{
  "viewport": {"width": 1280, "height": 720},
  "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
  "events": [
    {"t": 3.0, "x": 640, "y": 360, "type": "in"},
    {"t": 8.0, "x": 640, "y": 360, "type": "out"}
  ],
  "sync": {"t": 2.9, "x": 0, "y": 0, "size": 8}
}
```
`sync` is optional (see below). Old sidecars without `type` are read as alternating in/out.

### Time calibration (why there is a tiny magenta dot)

Events are stamped with the recorder's clock, but Playwright's video starts later by an amount
that varies (measured 0.4-0.9 s). So at the **first** zoom event the page draws an 8x8 px magenta
square in the top-left corner for 0.3 s and records when it did so (`sync.t`). `apply_zoom.py`
finds the first video frame showing it and shifts all events by the difference. In 6 test runs the
error dropped from ~0.53 s to <= 0.023 s (a frame is 0.04 s). Recordings without zoom events have
no marker. If the marker cannot be found, event times are used as recorded (warning on stderr);
`--time-offset` sets a manual shift.

## Services and API

| service | role |
|---------|------|
| `web` | Apache + mod_wsgi + Flask; `/media/` is served by Apache directly |
| `worker` | same image; takes `pending` videos from PostgreSQL (`FOR UPDATE SKIP LOCKED`), runs `apply_zoom`, writes `<name>.zoomed.webm` |
| `db` | PostgreSQL 16; new columns `zoom_events` (JSONB), `process_status`, `processed_video_url`, `processed_file_path`, `process_error`, `processed_at` |

| method | path | purpose |
|--------|------|---------|
| POST | `/upload` | `file`, `company_name`, optional `zoom` (sidecar). With a sidecar the video is queued automatically |
| POST | `/jobs/<id>/process` | queue the zoom step manually (202; 404 unknown; 409 already running) |
| GET | `/jobs/<id>`, `/jobs?company_name=` | `status` (none / pending / processing / done / failed), `processed_video_url` |
| GET | `/get_video_url`, `/videos`, `/media/<file>`, `/health` | as in the previous tasks |

```
docker compose exec db psql -U video_user -d videos -c "select id, company_name, process_status, processed_video_url from videos"
docker compose logs -f worker
```

## Tests

```
python -m pip install -r requirements-dev.txt
python -m pytest -v
```
* `test_zoom_logic.py`, `test_sidecar.py`, `test_apply_zoom_cli.py`, `test_qscript_zoom.py` - unit
  tests, no ffmpeg or browser needed (ramp frames, easing, clamping, multiple pairs, open-ended
  zoom, sidecar parsing, old sidecars, "no events -> copy", `--dry-run`, sync shift).
* `test_ffmpeg_integration.py` - renders real videos (skipped without ffmpeg).
* `test_stack_integration.py` - against the running stack (skipped when it is down):
  upload -> worker -> `processed_video_url` playable, `/jobs/<id>/process`, re-upload, and
  `ffmpeg` installed in the `web`/`worker` containers.
