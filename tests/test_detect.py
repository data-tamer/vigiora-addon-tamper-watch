"""The picture tests on synthetic pictures (tests/scenes.py), three different worlds each."""
import pytest
import scenes as S

from tamper_watch.detect import Thresholds, edge_match, is_covered, is_moved, measure, mode_of

TH = Thresholds()


def m(frame):
    return measure(frame, TH)


def ratio(frame, learned):
    return m(frame).sharpness / learned[1]


def match(frame, learned):
    return edge_match(learned[0], m(frame).edges)


# ---- covered ------------------------------------------------------------------------

@pytest.mark.parametrize("frame", [
    S.flat((0, 0, 0), 1), S.flat((128, 128, 128)), S.flat((30, 30, 200)), S.flat((250, 250, 250)),
    S.with_timestamp(S.flat((5, 5, 5), 1)), S.with_timestamp(S.flat((90, 140, 90))),
], ids=["black", "flat-grey", "flat-red", "flat-white-ir-glare", "black-with-timestamp", "flat-green-with-timestamp"])
def test_covered(frame):
    assert is_covered(m(frame), TH)


def test_normal_scene_is_not_covered(normal):
    assert not is_covered(m(normal), TH)


@pytest.mark.parametrize("factor", [0.25, 0.5, 1.5])
def test_darker_or_brighter_scene_is_not_covered(normal, factor):
    assert not is_covered(m(S.scale_brightness(normal, factor)), TH)


def test_night_grey_scene_is_not_covered(normal):
    assert not is_covered(m(S.add_noise(S.scale_brightness(S.to_grey_bgr(normal), 0.5), 8)), TH)


# ---- picture mode ---------------------------------------------------------------------

def test_mode_colour_and_grey(normal):
    assert mode_of(normal, TH) == "colour"
    assert mode_of(S.to_grey_bgr(normal), TH) == "grey"
    assert mode_of(S.to_grey_bgr(normal)[:, :, 0], TH) == "grey"


# ---- blurred --------------------------------------------------------------------------

@pytest.mark.parametrize("sigma", [2, 3, 5])
def test_blurred_scene_falls_below_the_ratio(normal, learned, sigma):
    assert ratio(S.blur(normal, sigma), learned) < TH.blur_ratio


@pytest.mark.parametrize("sigma,noise", [(2, 8), (3, 15)])
def test_blur_is_found_through_sensor_noise(normal, learned, sigma, noise):
    assert ratio(S.add_noise(S.blur(normal, sigma), noise), learned) < TH.blur_ratio


@pytest.mark.parametrize("change", ["darker", "brighter", "noise", "grey", "blob", "shifted"])
def test_sharpness_holds_for_ordinary_changes(world, normal, learned, change):
    frame = {
        "darker": S.scale_brightness(normal, 0.5), "brighter": S.scale_brightness(normal, 1.5),
        "noise": S.add_noise(normal, 15), "grey": S.to_grey_bgr(normal), "blob": S.with_blob(normal, 0.15),
        "shifted": S.view(world, dx=8),
    }[change]
    assert ratio(frame, learned) > 0.85


# ---- moved ----------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [dict(dx=3), dict(dx=8), dict(dx=-15), dict(dy=5), dict(dy=-10), dict(angle=10),
                                dict(angle=-15), dict(dx=4, angle=6)],
                         ids=["pan-3%", "pan-8%", "pan-15%-left", "tilt-5%", "tilt-10%-up", "rotate-10deg",
                              "rotate-15deg", "pan-and-rotate"])
def test_moved(world, learned, kw):
    mt = match(S.add_noise(S.view(world, **kw), 3, seed=7), learned)
    assert mt is not None and is_moved(mt, TH), mt


def test_moved_reports_the_direction(world, learned):
    mt = match(S.view(world, dx=8), learned)
    assert -10 < mt.shift_x < -6 and abs(mt.shift_y) < 1  # the view slid left: the camera turned right


@pytest.mark.parametrize("change", ["normal", "darker", "brighter", "noise8", "noise15", "grey", "grey-dark-noisy",
                                    "blob15-left", "blob15-centre", "blob15-right", "two-blobs"])
def test_not_moved_for_ordinary_changes(normal, learned, change):
    frame = {
        "normal": normal, "darker": S.scale_brightness(normal, 0.5), "brighter": S.scale_brightness(normal, 1.5),
        "noise8": S.add_noise(normal, 8), "noise15": S.add_noise(normal, 15), "grey": S.to_grey_bgr(normal),
        "grey-dark-noisy": S.add_noise(S.scale_brightness(S.to_grey_bgr(normal), 0.5), 10),
        "blob15-left": S.with_blob(normal, 0.15, 0.2), "blob15-centre": S.with_blob(normal, 0.15, 0.5),
        "blob15-right": S.with_blob(normal, 0.15, 0.8),
        "two-blobs": S.with_blob(S.with_blob(normal, 0.08, 0.25, seed=1), 0.08, 0.7, seed=2),
    }[change]
    mt = match(frame, learned)
    assert mt is not None and not is_moved(mt, TH), mt


def test_a_plain_view_is_not_judged_for_a_move():
    import numpy as np
    edges = m(S.flat((128, 128, 128))).edges
    plain = np.zeros(edges.shape, np.float32)  # a reference with no lasting edges
    assert edge_match(plain, edges) is None
