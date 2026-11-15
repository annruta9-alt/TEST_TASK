#!/usr/bin/env python3
"""Camtasia-style zoom post-processing with ffmpeg (zoompan).

Reads the sidecar written by qscript.py (<video>.zoom.json) and renders a new .webm
with a smooth zoom-in -> hold -> zoom-out around every recorded point.

    python apply_zoom.py --video video/ACME_GmbH.webm
    python apply_zoom.py --video video/ACME_GmbH.webm --dry-run
    python apply_zoom.py --video video/x.webm --ffmpeg C:\\tools\\ffmpeg\\bin\\ffmpeg.exe

The maths (events -> segments -> ffmpeg expressions) is pure Python and does not
need ffmpeg, so it can be unit-tested on its own.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_FPS = 25.0
DEFAULT_ZOOM = 2.0
DEFAULT_RAMP_SEC = 0.5
DEFAULT_VIEWPORT = (1280, 720)


# ------------------------------------------------------------------ sidecar parsing
def _number(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{name}' must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"'{name}' must be >= {minimum}, got {value!r}")
    return float(value)


def parse_sidecar(data) -> dict:
    """Validate/normalise sidecar data (already json-decoded).

    * a bare list is accepted as the list of events;
    * missing settings get defaults;
    * events are sorted by time;
    * old sidecars whose events have no "type" are treated as alternating toggles
      (in, out, in, out, ...), like the F5 key itself.
    """
    if isinstance(data, list):
        data = {"events": data}
    if not isinstance(data, dict):
        raise ValueError("sidecar must be a JSON object")

    viewport = data.get("viewport") or {}
    if not isinstance(viewport, dict):
        raise ValueError("'viewport' must be an object")
    width = int(_number(viewport.get("width", DEFAULT_VIEWPORT[0]), "viewport.width", 1))
    height = int(_number(viewport.get("height", DEFAULT_VIEWPORT[1]), "viewport.height", 1))
    fps = _number(data.get("fps", DEFAULT_FPS), "fps", 1)
    zoom = _number(data.get("zoom_factor", DEFAULT_ZOOM), "zoom_factor", 1)
    ramp = _number(data.get("ramp_sec", DEFAULT_RAMP_SEC), "ramp_sec", 0)

    raw = data.get("events") or []
    if not isinstance(raw, list):
        raise ValueError("'events' must be a list")
    events = []
    for i, ev in enumerate(raw):
        if not isinstance(ev, dict):
            raise ValueError(f"event #{i} must be an object")
        typ = ev.get("type")
        if typ is not None and typ not in ("in", "out"):
            raise ValueError(f"event #{i}: type must be 'in' or 'out', got {typ!r}")
        events.append({
            "t": _number(ev.get("t"), f"events[{i}].t", 0),
            "x": _number(ev.get("x"), f"events[{i}].x"),
            "y": _number(ev.get("y"), f"events[{i}].y"),
            "type": typ,
        })
    events.sort(key=lambda e: e["t"])  # stable: equal times keep file order

    zoomed = False
    for ev in events:  # fill in missing types by alternating
        if ev["type"] is None:
            ev["type"] = "out" if zoomed else "in"
        zoomed = ev["type"] == "in"

    sync = None
    raw_sync = data.get("sync")
    if raw_sync is not None:
        if not isinstance(raw_sync, dict):
            raise ValueError("'sync' must be an object")
        sync = {"t": _number(raw_sync.get("t"), "sync.t"),
                "x": int(_number(raw_sync.get("x", 0), "sync.x", 0)),
                "y": int(_number(raw_sync.get("y", 0), "sync.y", 0)),
                "size": int(_number(raw_sync.get("size", 8), "sync.size", 1))}

    return {"viewport": (width, height), "fps": fps, "zoom_factor": zoom,
            "ramp_sec": ramp, "events": events, "sync": sync}


def load_sidecar(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return parse_sidecar(json.load(fh))


def find_sidecar(video: str) -> Optional[str]:
    """<video stem>.zoom.json (what qscript writes) or <video file>.zoom.json."""
    stem = os.path.splitext(video)[0]
    for candidate in (stem + ".zoom.json", video + ".zoom.json"):
        if os.path.isfile(candidate):
            return candidate
    return None


# ------------------------------------------------------------------ zoom maths
@dataclass
class Segment:
    """One zoom: ramp in at f_in, hold, ramp out at f_out (None = hold to the end)."""
    f_in: int
    r_in: int
    f_out: Optional[int]
    r_out: int
    cx: float
    cy: float


def ease(u: float) -> float:
    """Smoothstep: slow start, slow end (no robotic linear ramp)."""
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def ramp_frames(ramp_sec: float, fps: float) -> int:
    return max(1, int(ramp_sec * fps + 0.5))


def map_point(x, y, viewport: Tuple[int, int], video_size: Tuple[int, int]):
    """Mouse (viewport px) -> video px. 1:1 when record_video_size == viewport."""
    return x * video_size[0] / viewport[0], y * video_size[1] / viewport[1]


def pair_events(events) -> List[tuple]:
    """Pair every 'in' with the next 'out' -> [(t_in, x, y, t_out | None)].

    A second 'in' while already zoomed and an 'out' while not zoomed are ignored.
    An 'in' without a following 'out' stays open (zoom holds to the end).
    """
    pairs, open_ev = [], None
    for ev in events:
        if ev["type"] == "in":
            if open_ev is None:
                open_ev = ev
        elif open_ev is not None:
            pairs.append((open_ev["t"], open_ev["x"], open_ev["y"], ev["t"]))
            open_ev = None
    if open_ev is not None:
        pairs.append((open_ev["t"], open_ev["x"], open_ev["y"], None))
    return pairs


def build_segments(events, fps: float, ramp_sec: float) -> List[Segment]:
    rf = ramp_frames(ramp_sec, fps)
    segs = []
    for t_in, x, y, t_out in pair_events(events):
        f_in = int(t_in * fps + 0.5)
        f_out = None if t_out is None else max(f_in, int(t_out * fps + 0.5))
        # hold shorter than the ramp: the zoom-in is shortened so it ends when
        # the user asks to zoom out
        r_in = rf if f_out is None else max(1, min(rf, f_out - f_in))
        segs.append(Segment(f_in, r_in, f_out, rf, x, y))
    # non-overlapping: the next zoom-in must not start before this zoom-out ended
    for prev, nxt in zip(segs, segs[1:]):
        prev.r_out = max(1, min(prev.r_out, nxt.f_in - prev.f_out))
    return segs


def level_at(frame: float, segs: List[Segment]) -> float:
    """0 = no zoom, 1 = full zoom (python twin of the ffmpeg expression)."""
    total = 0.0
    for s in segs:
        v = ease((frame - s.f_in) / s.r_in)
        if s.f_out is not None:
            v -= ease((frame - s.f_out) / s.r_out)
        total += v
    return min(1.0, max(0.0, total))


def zoom_at(frame: float, segs: List[Segment], factor: float) -> float:
    return 1.0 + (factor - 1.0) * level_at(frame, segs)


def center_at(frame: float, segs: List[Segment], size: Tuple[int, int]):
    """Centre of the latest zoom that has started (frame centre if there is none)."""
    if not segs:
        return size[0] / 2, size[1] / 2
    chosen = segs[0]
    for s in segs:
        if frame >= s.f_in:
            chosen = s
    return chosen.cx, chosen.cy


def crop_origin(cx, cy, zoom, width, height):
    """Top-left of the crop window, centred on (cx, cy) and clamped to the frame:
    x = clamp(cx - iw/(2*zoom), 0, iw - iw/zoom)."""
    cw, ch = width / zoom, height / zoom
    x = min(max(cx - cw / 2, 0.0), width - cw)
    y = min(max(cy - ch / 2, 0.0), height - ch)
    return x, y


# ------------------------------------------------------------------ ffmpeg filter
def _num(value) -> str:
    return format(value, ".6g")


def _smooth_expr(frame0: int, length: int) -> str:
    u = f"clip((on-{frame0})/{length},0,1)"
    return f"({u}*{u}*(3-2*{u}))"


def build_zoom_filter(segs: List[Segment], factor: float, width: int, height: int,
                      fps: float) -> Optional[str]:
    """The -vf string, or None when there is nothing to zoom."""
    if not segs:
        return None
    terms = []
    for s in segs:
        term = _smooth_expr(s.f_in, s.r_in)
        if s.f_out is not None:
            term = f"({term}-{_smooth_expr(s.f_out, s.r_out)})"
        terms.append(term)
    level = "+".join(terms)
    z_expr = f"1+{_num(factor - 1)}*({level})"

    cx, cy = _num(segs[0].cx), _num(segs[0].cy)
    for s in segs[1:]:
        cx = f"if(gte(on,{s.f_in}),{_num(s.cx)},{cx})"
        cy = f"if(gte(on,{s.f_in}),{_num(s.cy)},{cy})"
    x_expr = f"max(0,min({cx}-iw/zoom/2,iw-iw/zoom))"
    y_expr = f"max(0,min({cy}-ih/zoom/2,ih-ih/zoom))"

    fps_s = _num(fps)
    # fps first: constant frame rate, so "frame number == time * fps" holds
    return (f"fps={fps_s},zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
            f":d=1:s={width}x{height}:fps={fps_s}")


def build_command(ffmpeg, video, output, vf) -> List[str]:
    return [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", video, "-vf", vf,
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30",
        "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
        "-pix_fmt", "yuv420p", "-an", "-f", "webm", output,
    ]


def format_command(cmd: List[str]) -> str:
    """Copy-paste friendly command line for the current OS."""
    if os.name == "nt":
        def q(a):
            if a and not any(ch in a for ch in " ()',;&|<>^\""):
                return a
            return '"' + a.replace('"', '\\"') + '"'
        return " ".join(q(a) for a in cmd)

    def q(a):
        # readable: wrap in "..." when the argument only contains single quotes
        if a and not any(ch in a for ch in " ()',;&|<>^\"$`\\!*?[]{}#~"):
            return a
        if "'" in a and not any(ch in a for ch in '"$`\\!'):
            return '"' + a + '"'
        return shlex.quote(a)
    return " ".join(q(a) for a in cmd)


# ------------------------------------------------------------------ video probing
def _ffprobe_for(ffmpeg: str) -> Optional[str]:
    base = os.path.basename(ffmpeg).lower()
    if base.startswith("ffmpeg"):
        sibling = os.path.join(os.path.dirname(ffmpeg),
                               "ffprobe" + base[len("ffmpeg"):])
        if os.path.isfile(sibling):
            return sibling
    return shutil.which("ffprobe")


def probe_size(ffmpeg: str, video: str) -> Optional[Tuple[int, int]]:
    probe = _ffprobe_for(ffmpeg)
    if not probe or not os.path.isfile(video):
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", video],
            capture_output=True, text=True, timeout=60, check=True).stdout
        st = json.loads(out)["streams"][0]
        return int(st["width"]), int(st["height"])
    except Exception:
        return None


# ------------------------------------------------------------------ time calibration
# qscript.py stamps events with its own clock (t = seconds since the page was created).
# The video starts later, by an unpredictable amount. The page therefore draws a tiny
# magenta marker at the first zoom event and stores the time it did so ("sync.t");
# here we find the first video frame that shows it and shift all events accordingly.
def is_marker_pixel(r: float, g: float, b: float) -> bool:
    """Mean colour of the marker area: magenta (255, 0, 255), allowing for compression."""
    return r > 170 and b > 170 and g < 110


def find_marker_frame(raw: bytes, size: int) -> Optional[int]:
    """Index of the first frame whose size x size crop (raw rgb24 frames) is magenta."""
    frame_len = size * size * 3
    if frame_len <= 0:
        return None
    for index in range(len(raw) // frame_len):
        chunk = raw[index * frame_len:(index + 1) * frame_len]
        n = size * size
        r, g, b = sum(chunk[0::3]) / n, sum(chunk[1::3]) / n, sum(chunk[2::3]) / n
        if is_marker_pixel(r, g, b):
            return index
    return None


def detect_marker_time(ffmpeg: str, video: str, sync: dict, fps: float) -> Optional[float]:
    """Video time (s) of the first frame showing the sync marker, or None."""
    size = sync["size"]
    cmd = [ffmpeg, "-v", "error", "-i", video,
           "-vf", f"fps={_num(fps)},crop={size}:{size}:{sync['x']}:{sync['y']}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    index = find_marker_frame(res.stdout, size)
    return None if index is None else index / fps


def shift_events(events, offset: float):
    """video time = recorded time - offset (never below 0)."""
    return [{**ev, "t": max(0.0, ev["t"] - offset)} for ev in events]


# ------------------------------------------------------------------ main entry
def default_output(video: str) -> str:
    return os.path.splitext(video)[0] + ".zoomed.webm"


MAX_PLAUSIBLE_OFFSET = 10.0  # seconds; a bigger measured shift means a wrong detection


def process_video(video, sidecar=None, output=None, ffmpeg="ffmpeg", dry_run=False,
                  zoom_factor=None, ramp_sec=None, time_offset=None, use_sync=True) -> dict:
    """Render the zoomed video. Returns {"mode": ..., "output": ..., "command": ...}.

    mode: "zoomed", "copied" (no events -> input copied unchanged) or "dry-run".
    """
    output = output or default_output(video)
    if os.path.abspath(output) == os.path.abspath(video):
        raise ValueError("output must differ from the input video")
    sidecar = sidecar or find_sidecar(video)

    cfg = load_sidecar(sidecar) if sidecar else None
    events = cfg["events"] if cfg else []
    segs = []
    if cfg:
        fps = cfg["fps"]
        factor = zoom_factor or cfg["zoom_factor"]
        ramp = cfg["ramp_sec"] if ramp_sec is None else ramp_sec
        size = probe_size(ffmpeg, video) or cfg["viewport"]

        offset = time_offset  # manual override wins
        if offset is None and use_sync and cfg["sync"] and events:
            marker = detect_marker_time(ffmpeg, video, cfg["sync"], fps)
            if marker is None:
                print("warning: sync marker not found in the video - event times are "
                      "used as recorded", file=sys.stderr)
            else:
                offset = cfg["sync"]["t"] - marker
                if abs(offset) > MAX_PLAUSIBLE_OFFSET:
                    print(f"warning: implausible sync offset {offset:+.2f}s - ignored",
                          file=sys.stderr)
                    offset = None
                else:
                    print(f"sync: marker seen at {marker:.2f}s in the video, recorded at "
                          f"{cfg['sync']['t']:.2f}s -> events shifted by {-offset:+.2f}s",
                          file=sys.stderr)
        if offset:
            events = shift_events(events, offset)
        # coordinates -> video pixels (1:1 unless the video size differs from the viewport)
        mapped = []
        for ev in events:
            x, y = map_point(ev["x"], ev["y"], cfg["viewport"], size)
            mapped.append({**ev, "x": x, "y": y})
        segs = build_segments(mapped, fps, ramp)

    if not segs:  # nothing to zoom: copy unchanged
        if dry_run:
            print("no zoom events: the video would be copied unchanged")
            return {"mode": "dry-run", "output": output, "command": None, "segments": 0}
        shutil.copyfile(video, output)
        return {"mode": "copied", "output": output, "command": None, "segments": 0}

    vf = build_zoom_filter(segs, factor, size[0], size[1], fps)
    tmp = output + ".part"
    cmd = build_command(ffmpeg, video, tmp, vf)
    if dry_run:
        shown = list(cmd)
        shown[-1] = output
        print(format_command(shown))
        return {"mode": "dry-run", "output": output, "command": shown, "segments": len(segs)}

    if not (shutil.which(ffmpeg) or os.path.isfile(ffmpeg)):
        raise FileNotFoundError(
            f"ffmpeg not found ('{ffmpeg}'). Install it or pass --ffmpeg <path>.")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({res.returncode}): {res.stderr.strip()[-800:]}")
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"mode": "zoomed", "output": output, "command": cmd, "segments": len(segs)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply the recorded zoom events to a video.")
    ap.add_argument("--video", required=True, help="input .webm")
    ap.add_argument("--sidecar", help="zoom events JSON (default: <video>.zoom.json)")
    ap.add_argument("--output", help="output .webm (default: <video>.zoomed.webm)")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="path to the ffmpeg binary")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ffmpeg command, do not encode")
    ap.add_argument("--zoom-factor", type=float, help="override the sidecar zoom factor")
    ap.add_argument("--ramp-sec", type=float, help="override the sidecar ramp length")
    ap.add_argument("--time-offset", type=float,
                    help="seconds: video time = recorded time - offset (disables auto sync)")
    ap.add_argument("--no-sync", action="store_true",
                    help="do not align event times with the sync marker")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.video):
        print(f"error: video not found: {args.video}", file=sys.stderr)
        return 2
    try:
        result = process_video(args.video, args.sidecar, args.output, args.ffmpeg,
                               args.dry_run, args.zoom_factor, args.ramp_sec,
                               args.time_offset, not args.no_sync)
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result["mode"] != "dry-run":
        print(f"{result['mode']} ({result['segments']} zoom(s)): {result['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())