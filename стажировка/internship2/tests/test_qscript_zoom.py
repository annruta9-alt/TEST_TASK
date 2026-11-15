"""Zoom capture helpers of qscript.py (no browser needed)."""
import json
import os

import pytest

pytest.importorskip("playwright")
import qscript  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    qscript._zoom.update(t0=100.0, state="out", events=[], sync_t=None)
    yield
    qscript._zoom.update(t0=0.0, state="out", events=[], sync_t=None)


def test_toggle_alternates_in_out():
    types = [qscript._on_zoom_event((100 + i) * 1000, 10, 10)["type"] for i in range(1, 5)]
    assert types == ["in", "out", "in", "out"]
    assert [e["type"] for e in qscript._zoom["events"]] == types


def test_first_event_is_flagged_for_the_sync_marker():
    assert qscript._on_zoom_event(101_000, 1, 1)["first"] is True
    assert qscript._on_zoom_event(102_000, 1, 1)["first"] is False


def test_event_time_is_relative_to_t0():
    qscript._on_zoom_event(103_000, 640, 360)            # epoch 103 s, t0 = 100 s
    assert qscript._zoom["events"][0] == {"t": 3.0, "x": 640, "y": 360, "type": "in"}


def test_event_format_matches_the_sidecar_spec():
    qscript._on_zoom_event(101_500, 640.4, 359.6)
    ev = qscript._zoom["events"][0]
    assert set(ev) == {"t", "x", "y", "type"}
    assert isinstance(ev["x"], int) and isinstance(ev["y"], int) and ev["t"] == 1.5


def test_coordinates_are_clamped_to_the_viewport():
    qscript._on_zoom_event(101_000, -50, 99999)
    ev = qscript._zoom["events"][0]
    assert (ev["x"], ev["y"]) == (0, qscript.VIEWPORT["height"] - 1)


def test_time_never_negative():
    qscript._on_zoom_event(50_000, 1, 1)
    assert qscript._zoom["events"][0]["t"] == 0.0


def test_sync_marker_time_is_relative_to_t0():
    qscript._on_sync_marker(102_250)
    assert qscript._zoom["sync_t"] == 2.25


def test_sidecar_is_written_only_when_there_are_events(tmp_path):
    video = str(tmp_path / "ACME.webm")
    assert qscript._write_zoom_sidecar(video, []) is None
    assert not (tmp_path / "ACME.zoom.json").exists()


def test_sidecar_content_matches_the_spec(tmp_path):
    video = str(tmp_path / "ACME.webm")
    events = [{"t": 3.0, "x": 640, "y": 360, "type": "in"},
              {"t": 8.0, "x": 640, "y": 360, "type": "out"}]
    path = qscript._write_zoom_sidecar(video, events)
    assert path.endswith("ACME.zoom.json")
    data = json.load(open(path, encoding="utf-8"))
    assert data["viewport"] == {"width": 1280, "height": 720}
    assert data["fps"] == 25 and data["zoom_factor"] == 2.0 and data["ramp_sec"] == 0.5
    assert data["events"] == events and "sync" not in data


def test_sidecar_includes_the_sync_time_when_known(tmp_path):
    path = qscript._write_zoom_sidecar(str(tmp_path / "a.webm"),
                                       [{"t": 1, "x": 1, "y": 1, "type": "in"}], sync_t=1.25)
    assert json.load(open(path))["sync"] == {"t": 1.25, "x": 0, "y": 0, "size": 8}


def test_stale_sidecar_is_removed_when_a_new_recording_has_no_events(tmp_path):
    video = str(tmp_path / "ACME.webm")
    (tmp_path / "ACME.zoom.json").write_text("{}")
    qscript._write_zoom_sidecar(video, [])
    assert not (tmp_path / "ACME.zoom.json").exists()


def test_the_sidecar_it_writes_is_readable_by_apply_zoom(tmp_path):
    import apply_zoom
    path = qscript._write_zoom_sidecar(
        str(tmp_path / "a.webm"),
        [{"t": 1.0, "x": 10, "y": 20, "type": "in"}, {"t": 4.0, "x": 10, "y": 20, "type": "out"}],
        sync_t=0.9)
    cfg = apply_zoom.load_sidecar(path)
    assert len(cfg["events"]) == 2 and cfg["sync"]["t"] == 0.9


def test_zoomat_without_a_page_does_nothing():
    assert qscript._page is None and qscript.ZoomAt(10, 10) is None


def test_page_script_has_the_f5_hotkey_and_the_programmatic_hook():
    js = qscript.OVERLAY_JS
    assert "e.key === 'F5'" in js and "preventDefault" in js     # F5 must not reload
    assert "window.__zoomAt" in js and "__pwZoomEvent" in js
    assert "#00c853" in js and "#ff1744" in js                   # green = in, red = out
