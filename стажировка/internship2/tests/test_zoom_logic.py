"""Unit tests for the zoom maths (no ffmpeg needed)."""
import pytest

import apply_zoom as az

FPS = 25


def ev(t, typ, x=640, y=360):
    return {"t": t, "x": x, "y": y, "type": typ}


# ----------------------------------------------------------------- ramp / easing
def test_ramp_frames():
    assert az.ramp_frames(0.5, 25) == 13
    assert az.ramp_frames(1.0, 30) == 30
    assert az.ramp_frames(0, 25) == 1          # never 0 frames (division by zero)


def test_ease_is_smoothstep():
    assert az.ease(0) == 0 and az.ease(1) == 1
    assert az.ease(0.5) == pytest.approx(0.5)
    assert az.ease(-3) == 0 and az.ease(7) == 1          # clamped
    assert az.ease(0.1) < 0.1 and az.ease(0.9) > 0.9     # slow start, slow end


def test_ease_is_monotonic():
    values = [az.ease(i / 100) for i in range(101)]
    assert values == sorted(values)


# ----------------------------------------------------------------- pairing
def test_pair_in_out():
    pairs = az.pair_events([ev(3.0, "in"), ev(8.0, "out")])
    assert pairs == [(3.0, 640, 360, 8.0)]


def test_multiple_pairs():
    pairs = az.pair_events([ev(1, "in", 10, 20), ev(2, "out"), ev(5, "in", 30, 40), ev(6, "out")])
    assert pairs == [(1, 10, 20, 2), (5, 30, 40, 6)]


def test_open_ended_pair_has_no_out():
    assert az.pair_events([ev(1, "in"), ev(2, "out"), ev(4, "in", 5, 6)])[-1] == (4, 5, 6, None)


def test_duplicate_in_and_stray_out_are_ignored():
    events = [ev(0.5, "out"), ev(1, "in", 1, 1), ev(2, "in", 2, 2), ev(3, "out"), ev(4, "out")]
    assert az.pair_events(events) == [(1, 1, 1, 3)]


# ----------------------------------------------------------------- segments (frames)
def test_segment_frames_and_ramps():
    (seg,) = az.build_segments([ev(3.0, "in"), ev(8.0, "out")], FPS, 0.5)
    assert (seg.f_in, seg.f_out) == (75, 200)
    assert (seg.r_in, seg.r_out) == (13, 13)


def test_open_ended_segment():
    (seg,) = az.build_segments([ev(2.0, "in")], FPS, 0.5)
    assert seg.f_out is None


def test_short_hold_shortens_the_zoom_in():
    (seg,) = az.build_segments([ev(1.0, "in"), ev(1.2, "out")], FPS, 0.5)  # hold 5 frames
    assert seg.r_in == 5 and seg.r_out == 13


def test_next_zoom_in_shortens_previous_zoom_out():
    a, b = az.build_segments(
        [ev(1, "in"), ev(3, "out"), ev(3.2, "in"), ev(5, "out")], FPS, 0.5)
    assert a.f_out + a.r_out <= b.f_in           # non-overlapping
    assert a.r_out == b.f_in - a.f_out == 5


# ----------------------------------------------------------------- zoom level over time
@pytest.fixture
def one_pair():
    return az.build_segments([ev(3.0, "in"), ev(8.0, "out")], FPS, 0.5)


def test_level_before_during_after(one_pair):
    assert az.level_at(0, one_pair) == 0
    assert az.level_at(75, one_pair) == 0                         # ramp starts
    assert az.level_at(75 + 6.5, one_pair) == pytest.approx(0.5)  # middle of the ramp
    assert az.level_at(75 + 13, one_pair) == pytest.approx(1)     # ramp done
    assert az.level_at(140, one_pair) == pytest.approx(1)         # hold
    assert az.level_at(200, one_pair) == pytest.approx(1)         # zoom-out starts here
    assert az.level_at(200 + 13, one_pair) == pytest.approx(0)
    assert az.level_at(240, one_pair) == 0


def test_ramp_is_eased_not_linear(one_pair):
    assert az.level_at(75 + 1, one_pair) < 1 / 13                 # slow start
    assert az.level_at(75 + 12, one_pair) > 12 / 13               # slow end
    levels = [az.level_at(f, one_pair) for f in range(75, 89)]
    assert levels == sorted(levels)                               # never goes back


def test_zoom_factor_applied(one_pair):
    assert az.zoom_at(0, one_pair, 2.0) == 1.0
    assert az.zoom_at(140, one_pair, 2.0) == pytest.approx(2.0)
    assert az.zoom_at(140, one_pair, 3.5) == pytest.approx(3.5)


def test_open_ended_zoom_holds_to_the_end():
    segs = az.build_segments([ev(2.0, "in")], FPS, 0.5)
    assert az.level_at(10 * FPS, segs) == pytest.approx(1)
    assert az.level_at(10_000, segs) == pytest.approx(1)


def test_multiple_pairs_return_to_normal_between():
    segs = az.build_segments([ev(1, "in"), ev(3, "out"), ev(6, "in"), ev(8, "out")], FPS, 0.5)
    assert az.level_at(2 * FPS, segs) == pytest.approx(1)
    assert az.level_at(4.5 * FPS, segs) == pytest.approx(0)       # between the pairs
    assert az.level_at(7 * FPS, segs) == pytest.approx(1)
    assert az.level_at(9.5 * FPS, segs) == pytest.approx(0)


def test_level_never_exceeds_one_even_with_back_to_back_pairs():
    segs = az.build_segments([ev(1, "in"), ev(2, "out"), ev(2, "in"), ev(3, "out")], FPS, 0.5)
    assert all(0 <= az.level_at(f, segs) <= 1 for f in range(0, 120))


def test_center_follows_the_latest_started_zoom():
    segs = az.build_segments(
        [ev(1, "in", 100, 100), ev(2, "out"), ev(5, "in", 900, 600), ev(6, "out")], FPS, 0.5)
    assert az.center_at(2 * FPS, segs, (1280, 720)) == (100, 100)
    assert az.center_at(5.5 * FPS, segs, (1280, 720)) == (900, 600)
    assert az.center_at(0, [], (1280, 720)) == (640, 360)


# ----------------------------------------------------------------- coordinates
def test_crop_is_centred_on_the_point():
    assert az.crop_origin(640, 360, 2.0, 1280, 720) == (320, 180)
    assert az.crop_origin(900, 200, 2.0, 1280, 720) == (580, 20)


def test_crop_is_clamped_to_the_frame():
    assert az.crop_origin(0, 0, 2.0, 1280, 720) == (0, 0)
    assert az.crop_origin(1280, 720, 2.0, 1280, 720) == (640, 360)
    assert az.crop_origin(1240, 30, 2.0, 1280, 720) == (640, 0)
    for z in (1.0, 1.5, 2.0, 4.0):
        for cx, cy in ((0, 0), (1279, 719), (640, 360), (5000, -5000)):
            x, y = az.crop_origin(cx, cy, z, 1280, 720)
            assert 0 <= x <= 1280 - 1280 / z + 1e-9 and 0 <= y <= 720 - 720 / z + 1e-9


def test_no_zoom_means_full_frame():
    assert az.crop_origin(900, 200, 1.0, 1280, 720) == (0, 0)


def test_mouse_to_video_mapping_is_1_to_1_when_sizes_match():
    assert az.map_point(900, 200, (1280, 720), (1280, 720)) == (900, 200)


def test_mouse_to_video_mapping_scales_when_video_differs():
    assert az.map_point(640, 360, (1280, 720), (1920, 1080)) == (960, 540)


# ----------------------------------------------------------------- ffmpeg filter
def test_no_segments_no_filter():
    assert az.build_zoom_filter([], 2.0, 1280, 720, FPS) is None


def test_filter_structure(one_pair):
    vf = az.build_zoom_filter(one_pair, 2.0, 1280, 720, FPS)
    assert vf.startswith("fps=25,zoompan=")        # constant frame rate first
    assert ":d=1:s=1280x720:fps=25" in vf            # resolution preserved
    assert "max(0,min(640-iw/zoom/2,iw-iw/zoom))" in vf   # centred + clamped (x)
    assert "max(0,min(360-ih/zoom/2,ih-ih/zoom))" in vf   # centred + clamped (y)
    assert vf.count("clip(") == 6                    # in-ramp and out-ramp, 3 uses each
    assert "(on-75)" in vf and "(on-200)" in vf


def test_open_ended_filter_has_no_zoom_out_term():
    vf = az.build_zoom_filter(az.build_segments([ev(2, "in")], FPS, 0.5), 2.0, 1280, 720, FPS)
    assert vf.count("clip(") == 3 and "(on-50)" in vf


def test_multiple_pairs_switch_the_centre():
    segs = az.build_segments(
        [ev(1, "in", 100, 50), ev(2, "out"), ev(5, "in", 900, 600), ev(6, "out")], FPS, 0.5)
    vf = az.build_zoom_filter(segs, 2.0, 1280, 720, FPS)
    assert "if(gte(on,125),900,100)" in vf and "if(gte(on,125),600,50)" in vf


def test_zoom_factor_in_filter():
    vf = az.build_zoom_filter(az.build_segments([ev(1, "in")], FPS, 0.5), 3.0, 1280, 720, FPS)
    assert "z='1+2*(" in vf
