"""apply_zoom.py command line / process_video paths that do not need ffmpeg."""
import json
import os

import pytest

import apply_zoom as az

FAKE_FFMPEG = "/definitely/not/installed/ffmpeg"


def write_sidecar(path, events, sync=None, **extra):
    data = {"viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0,
            "ramp_sec": 0.5, "events": events, **extra}
    if sync:
        data["sync"] = sync
    path.write_text(json.dumps(data))


@pytest.fixture
def video(tmp_path):
    v = tmp_path / "ACME.webm"
    v.write_bytes(b"not really a video, only bytes")
    return v


def test_no_events_copies_the_video_unchanged(tmp_path, video):
    write_sidecar(tmp_path / "ACME.zoom.json", [])
    out = tmp_path / "out.webm"
    result = az.process_video(str(video), output=str(out), ffmpeg=FAKE_FFMPEG)
    assert result["mode"] == "copied"
    assert out.read_bytes() == video.read_bytes()          # byte-for-byte, ffmpeg not needed


def test_missing_sidecar_copies_the_video_unchanged(video):
    result = az.process_video(str(video), ffmpeg=FAKE_FFMPEG)
    assert result["mode"] == "copied"
    assert open(result["output"], "rb").read() == video.read_bytes()
    assert result["output"].endswith("ACME.zoomed.webm")


def test_only_stray_out_events_means_nothing_to_zoom(tmp_path, video):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 1, "x": 1, "y": 1, "type": "out"}])
    assert az.process_video(str(video), ffmpeg=FAKE_FFMPEG)["mode"] == "copied"


def test_output_must_differ_from_input(video):
    with pytest.raises(ValueError):
        az.process_video(str(video), output=str(video))


def test_missing_ffmpeg_gives_a_clear_error(tmp_path, video):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 1, "x": 5, "y": 5, "type": "in"}])
    with pytest.raises(FileNotFoundError, match="--ffmpeg"):
        az.process_video(str(video), ffmpeg=FAKE_FFMPEG)


def test_dry_run_prints_the_command_and_does_not_encode(tmp_path, video, capsys):
    write_sidecar(tmp_path / "ACME.zoom.json",
                  [{"t": 3, "x": 640, "y": 360, "type": "in"},
                   {"t": 8, "x": 640, "y": 360, "type": "out"}])
    rc = az.main(["--video", str(video), "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr().out
    assert rc == 0
    assert FAKE_FFMPEG in printed and "zoompan" in printed and "libvpx-vp9" in printed
    assert "ACME.zoomed.webm" in printed
    assert not (tmp_path / "ACME.zoomed.webm").exists()
    assert not (tmp_path / "ACME.zoomed.webm.part").exists()


def test_dry_run_without_events_says_copy(tmp_path, video, capsys):
    write_sidecar(tmp_path / "ACME.zoom.json", [])
    assert az.main(["--video", str(video), "--dry-run"]) == 0
    assert "copied unchanged" in capsys.readouterr().out
    assert not (tmp_path / "ACME.zoomed.webm").exists()


def test_explicit_sidecar_and_output_options(tmp_path, video, capsys):
    side = tmp_path / "elsewhere.json"
    write_sidecar(side, [{"t": 2, "x": 100, "y": 100, "type": "in"}])
    out = tmp_path / "custom.webm"
    az.main(["--video", str(video), "--sidecar", str(side), "--output", str(out),
             "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    assert "custom.webm" in capsys.readouterr().out


def test_zoom_factor_and_ramp_overrides(tmp_path, video, capsys):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 2, "x": 100, "y": 100, "type": "in"}])
    az.main(["--video", str(video), "--dry-run", "--zoom-factor", "3", "--ramp-sec", "1",
             "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr().out
    assert "z=" in printed and "1+2*(" in printed and "/25," in printed   # 1 s ramp = 25 frames


def test_missing_video_is_an_error(tmp_path, capsys):
    assert az.main(["--video", str(tmp_path / "nope.webm")]) == 2


def test_broken_sidecar_is_an_error(tmp_path, video, capsys):
    (tmp_path / "ACME.zoom.json").write_text("{not json")
    assert az.main(["--video", str(video), "--dry-run"]) == 2
    assert "error" in capsys.readouterr().err


# ----------------------------------------------------------------- sync marker -> time shift
def test_sync_marker_shifts_event_times(tmp_path, video, capsys, monkeypatch):
    # recorded: marker drawn at 1.0 s, zoom-in at 3.0 s; video shows the marker at 0.4 s
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 3.0, "x": 640, "y": 360, "type": "in"}],
                  sync={"t": 1.0, "x": 0, "y": 0, "size": 8})
    monkeypatch.setattr(az, "detect_marker_time", lambda *a, **k: 0.4)
    az.main(["--video", str(video), "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr()
    assert "(on-60)" in printed.out          # 3.0 - 0.6 = 2.4 s -> frame 60 (not 75)
    assert "shifted by -0.60s" in printed.err


def test_no_sync_option_uses_times_as_recorded(tmp_path, video, capsys, monkeypatch):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 3.0, "x": 640, "y": 360, "type": "in"}],
                  sync={"t": 1.0})
    monkeypatch.setattr(az, "detect_marker_time", lambda *a, **k: 0.4)
    az.main(["--video", str(video), "--dry-run", "--no-sync", "--ffmpeg", FAKE_FFMPEG])
    assert "(on-75)" in capsys.readouterr().out


def test_manual_time_offset_wins_over_sync(tmp_path, video, capsys, monkeypatch):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 3.0, "x": 640, "y": 360, "type": "in"}],
                  sync={"t": 1.0})
    monkeypatch.setattr(az, "detect_marker_time", lambda *a, **k: 0.4)
    az.main(["--video", str(video), "--dry-run", "--time-offset", "1.0", "--ffmpeg", FAKE_FFMPEG])
    assert "(on-50)" in capsys.readouterr().out        # 3.0 - 1.0 = 2.0 s


def test_marker_not_found_falls_back_to_recorded_times(tmp_path, video, capsys, monkeypatch):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 3.0, "x": 640, "y": 360, "type": "in"}],
                  sync={"t": 1.0})
    monkeypatch.setattr(az, "detect_marker_time", lambda *a, **k: None)
    az.main(["--video", str(video), "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr()
    assert "(on-75)" in printed.out and "warning" in printed.err


def test_implausible_offset_is_ignored(tmp_path, video, capsys, monkeypatch):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 30.0, "x": 640, "y": 360, "type": "in"}],
                  sync={"t": 25.0})
    monkeypatch.setattr(az, "detect_marker_time", lambda *a, **k: 0.2)
    az.main(["--video", str(video), "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr()
    assert "(on-750)" in printed.out and "implausible" in printed.err


def test_video_size_other_than_viewport_scales_the_points(tmp_path, video, capsys, monkeypatch):
    write_sidecar(tmp_path / "ACME.zoom.json", [{"t": 1, "x": 640, "y": 360, "type": "in"}])
    monkeypatch.setattr(az, "probe_size", lambda *a, **k: (1920, 1080))
    az.main(["--video", str(video), "--dry-run", "--ffmpeg", FAKE_FFMPEG])
    printed = capsys.readouterr().out
    assert "s=1920x1080" in printed and "960-iw/zoom/2" in printed and "540-ih/zoom/2" in printed
