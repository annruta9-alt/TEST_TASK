"""Sidecar parsing, time calibration helpers, sidecar lookup."""
import json

import pytest

import apply_zoom as az

SPEC_EXAMPLE = {
    "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
    "events": [{"t": 3.0, "x": 640, "y": 360, "type": "in"},
               {"t": 8.0, "x": 640, "y": 360, "type": "out"}],
}


def test_parse_spec_example():
    cfg = az.parse_sidecar(SPEC_EXAMPLE)
    assert cfg["viewport"] == (1280, 720) and cfg["fps"] == 25
    assert cfg["zoom_factor"] == 2.0 and cfg["ramp_sec"] == 0.5
    assert [e["type"] for e in cfg["events"]] == ["in", "out"]
    assert cfg["sync"] is None


def test_defaults_for_missing_settings():
    cfg = az.parse_sidecar({"events": [{"t": 1, "x": 1, "y": 2, "type": "in"}]})
    assert cfg["viewport"] == (1280, 720)
    assert cfg["fps"] == 25 and cfg["zoom_factor"] == 2.0 and cfg["ramp_sec"] == 0.5


def test_old_sidecar_without_type_alternates_in_out():
    cfg = az.parse_sidecar({"events": [{"t": 1, "x": 1, "y": 1}, {"t": 2, "x": 1, "y": 1},
                                       {"t": 4, "x": 1, "y": 1}, {"t": 5, "x": 1, "y": 1}]})
    assert [e["type"] for e in cfg["events"]] == ["in", "out", "in", "out"]


def test_old_sidecar_odd_number_of_events_is_open_ended():
    cfg = az.parse_sidecar({"events": [{"t": 1, "x": 1, "y": 1}, {"t": 2, "x": 1, "y": 1},
                                       {"t": 4, "x": 1, "y": 1}]})
    assert [e["type"] for e in cfg["events"]] == ["in", "out", "in"]
    assert az.pair_events(cfg["events"])[-1][3] is None


def test_partially_typed_events_continue_from_the_known_state():
    cfg = az.parse_sidecar({"events": [{"t": 1, "x": 1, "y": 1, "type": "in"},
                                       {"t": 2, "x": 1, "y": 1}]})
    assert [e["type"] for e in cfg["events"]] == ["in", "out"]


def test_events_are_sorted_by_time():
    cfg = az.parse_sidecar({"events": [{"t": 5, "x": 1, "y": 1, "type": "out"},
                                       {"t": 2, "x": 1, "y": 1, "type": "in"}]})
    assert [e["t"] for e in cfg["events"]] == [2, 5]


def test_bare_list_is_accepted_as_events():
    cfg = az.parse_sidecar([{"t": 1, "x": 1, "y": 1}])
    assert len(cfg["events"]) == 1


def test_empty_sidecar_has_no_events():
    assert az.parse_sidecar({})["events"] == []
    assert az.parse_sidecar({"events": []})["events"] == []


@pytest.mark.parametrize("bad", [
    "text", 5, {"events": "nope"}, {"events": [1]},
    {"events": [{"x": 1, "y": 1}]},                              # no t
    {"events": [{"t": -1, "x": 1, "y": 1}]},                     # negative t
    {"events": [{"t": 1, "x": 1, "y": 1, "type": "zoom"}]},      # unknown type
    {"events": [{"t": True, "x": 1, "y": 1}]},                   # bool is not a number
    {"fps": 0, "events": []}, {"zoom_factor": 0.5, "events": []},
    {"viewport": {"width": 0, "height": 5}, "events": []},
])
def test_invalid_sidecars_raise(bad):
    with pytest.raises(ValueError):
        az.parse_sidecar(bad)


def test_sync_section_is_parsed():
    cfg = az.parse_sidecar({"events": [], "sync": {"t": 2.5}})
    assert cfg["sync"] == {"t": 2.5, "x": 0, "y": 0, "size": 8}


def test_load_sidecar_reads_utf8_bom(tmp_path):
    path = tmp_path / "a.zoom.json"
    path.write_text(json.dumps(SPEC_EXAMPLE), encoding="utf-8-sig")
    assert len(az.load_sidecar(str(path))["events"]) == 2


def test_find_sidecar_prefers_the_stem_name(tmp_path):
    video = tmp_path / "ACME.webm"
    video.write_bytes(b"x")
    assert az.find_sidecar(str(video)) is None
    (tmp_path / "ACME.webm.zoom.json").write_text("{}")
    assert az.find_sidecar(str(video)).endswith("ACME.webm.zoom.json")
    (tmp_path / "ACME.zoom.json").write_text("{}")
    assert az.find_sidecar(str(video)).endswith("ACME.zoom.json")


# ----------------------------------------------------------------- time calibration
def test_shift_events():
    out = az.shift_events([{"t": 3.0, "x": 1, "y": 1, "type": "in"},
                           {"t": 0.2, "x": 1, "y": 1, "type": "out"}], 0.6)
    assert [round(e["t"], 3) for e in out] == [2.4, 0.0]     # never negative


def _frames(colors, size=8):
    return b"".join(bytes(c) * (size * size) for c in colors)


def test_find_marker_frame():
    raw = _frames([(255, 255, 255)] * 10 + [(250, 20, 240)] * 4 + [(255, 255, 255)] * 3)
    assert az.find_marker_frame(raw, 8) == 10


def test_marker_not_found():
    assert az.find_marker_frame(_frames([(255, 255, 255)] * 5 + [(255, 0, 0)] * 5), 8) is None
    assert az.find_marker_frame(b"", 8) is None


def test_marker_colour_test():
    assert az.is_marker_pixel(255, 0, 255) and az.is_marker_pixel(230, 40, 225)
    assert not az.is_marker_pixel(255, 255, 255)      # white page
    assert not az.is_marker_pixel(255, 0, 0)          # red
    assert not az.is_marker_pixel(0, 0, 0)            # black
