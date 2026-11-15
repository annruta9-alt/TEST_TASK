"""Integration: really renders videos with ffmpeg (skipped when ffmpeg is not installed)."""
import json
import shutil
import subprocess

import pytest

import apply_zoom as az

FFMPEG = shutil.which("ffmpeg")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")]

W, H = 1280, 720


def make_video(path, seconds=8, extra_filters=""):
    """Black 1280x720 video with a red 20x20 box centred on (900, 200)."""
    flt = "drawbox=x=890:y=190:w=20:h=20:color=red:t=fill" + extra_filters
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s={W}x{H}:r=25:d={seconds},{flt}",
                    "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30", "-pix_fmt", "yuv420p",
                    str(path)], check=True)


def red_box(video, t):
    """(centre x, centre y, width) of the red pixels in the frame at time t."""
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    xs, ys = [], []
    for i in range(0, len(raw), 3):
        if raw[i] > 150 and raw[i + 1] < 80 and raw[i + 2] < 80:
            p = i // 3
            xs.append(p % W)
            ys.append(p // W)
    return (sum(xs) / len(xs), sum(ys) / len(ys), max(xs) - min(xs) + 1) if xs else None


def sidecar(tmp_path, events, **extra):
    path = tmp_path / "src.zoom.json"
    path.write_text(json.dumps({"viewport": {"width": W, "height": H}, "fps": 25,
                                "zoom_factor": 2.0, "ramp_sec": 0.5, "events": events, **extra}))


def probe(video):
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,codec_name",
                          "-show_entries", "format=duration", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return data["streams"][0], float(data["format"]["duration"])


def test_zoomed_video_is_centred_and_keeps_resolution(tmp_path):
    src = tmp_path / "src.webm"
    make_video(src)
    sidecar(tmp_path, [{"t": 2, "x": 900, "y": 200, "type": "in"},
                       {"t": 5, "x": 900, "y": 200, "type": "out"}])
    result = az.process_video(str(src), ffmpeg=FFMPEG)
    assert result["mode"] == "zoomed"
    stream, duration = probe(result["output"])
    assert (stream["width"], stream["height"], stream["codec_name"]) == (W, H, "vp9")
    assert duration == pytest.approx(8.0, abs=0.1)

    cx, cy, width = red_box(result["output"], 3.5)       # holding the zoom
    assert (cx, cy) == pytest.approx((640, 360), abs=1.5)  # exact mouse point -> frame centre
    assert width == pytest.approx(40, abs=2)               # 2x
    assert red_box(result["output"], 1.0) == pytest.approx((900, 200, 20), abs=1.5)  # before
    assert red_box(result["output"], 7.0) == pytest.approx((900, 200, 20), abs=1.5)  # after


def test_open_ended_zoom_holds_to_the_last_frame(tmp_path):
    src = tmp_path / "src.webm"
    make_video(src)
    sidecar(tmp_path, [{"t": 3, "x": 900, "y": 200, "type": "in"}])
    out = az.process_video(str(src), ffmpeg=FFMPEG)["output"]
    assert red_box(out, 7.9)[2] == pytest.approx(40, abs=2)


def test_crop_stays_inside_the_frame_near_the_corner(tmp_path):
    src = tmp_path / "src.webm"
    make_video(src, extra_filters=",drawbox=x=30:y=20:w=20:h=20:color=red:t=fill")
    sidecar(tmp_path, [{"t": 1, "x": 40, "y": 30, "type": "in"}])
    out = az.process_video(str(src), ffmpeg=FFMPEG)["output"]
    # zoomed on the corner: crop clamped to (0, 0) -> the corner box lands at (80, 60)
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", "4", "-i", out, "-frames:v", "1",
                          "-vf", "crop=100:100:30:10", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    assert any(raw[i] > 150 and raw[i + 1] < 80 for i in range(0, len(raw), 3))


def test_sync_marker_is_found_and_shifts_the_zoom(tmp_path):
    src = tmp_path / "src.webm"
    # magenta 8x8 marker visible 0.4 s .. 0.8 s of the video
    make_video(src, extra_filters=",drawbox=x=0:y=0:w=8:h=8:color=magenta:t=fill:enable='between(t,0.4,0.8)'")
    marker = az.detect_marker_time(FFMPEG, str(src), {"x": 0, "y": 0, "size": 8}, 25)
    assert marker == pytest.approx(0.4, abs=0.08)
    # recorded: marker at 1.0 s, zoom-in at 3.0 s -> the zoom must start at 2.4 s of the video
    sidecar(tmp_path, [{"t": 3.0, "x": 900, "y": 200, "type": "in"}],
            sync={"t": 1.0, "x": 0, "y": 0, "size": 8})
    out = az.process_video(str(src), ffmpeg=FFMPEG)["output"]
    assert red_box(out, 2.2)[2] == pytest.approx(20, abs=1.5)    # not zoomed yet
    assert red_box(out, 3.4)[2] == pytest.approx(40, abs=2)       # fully zoomed (2.4 s + ramp)
    # without calibration the zoom would still be at 20 px here (it would start at 3.0 s)


def test_no_events_copy_keeps_the_exact_file(tmp_path):
    src = tmp_path / "src.webm"
    make_video(src, seconds=2)
    sidecar(tmp_path, [])
    out = az.process_video(str(src), ffmpeg=FFMPEG)["output"]
    assert open(out, "rb").read() == src.read_bytes()
