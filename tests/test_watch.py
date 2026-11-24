"""The per-camera state machine: learning, persistence, one event per change, recovery, saving."""
import pytest
import scenes as S
from conftest import learning_frames

from tamper_watch.detect import Thresholds, measure
from tamper_watch.watch import COVERED, LEARNING, MOVED, OK, CameraWatch, Checks

TH = Thresholds()
ALL = Checks()


@pytest.fixture(scope="module")
def w():
    return S.world(2)


@pytest.fixture(scope="module")
def frames(w):
    n = S.add_noise(S.view(w), 3, seed=999)
    return {
        "learn": [measure(f, TH) for f in learning_frames(w)],
        "normal": measure(n, TH),
        "busy": measure(S.with_blob(n, 0.15, 0.4), TH),
        "black": measure(S.flat((0, 0, 0), 1), TH),
        "blurred": measure(S.blur(n, 4), TH),
        "moved": measure(S.view(w, dx=10), TH),
        "grey": measure(S.to_grey_bgr(n), TH),
        "grey_moved": measure(S.to_grey_bgr(S.view(w, dx=10)), TH),
    }


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        self.t += 30
        return self.t


def learned_watch(frames, **kw):
    cw = CameraWatch(7, TH, **kw)
    for m in frames["learn"]:
        assert cw.observe(m, ALL) is None
    assert not cw.learning("colour")
    return cw


def feed(cw, ms, checks=ALL, clock=None):
    clock = clock or Clock()
    return [t for t in (cw.observe(m, checks, clock()) for m in ms) if t is not None]


def test_learning_gives_no_verdict_but_covered(frames):
    cw = CameraWatch(7, TH)
    assert cw.assess(frames["moved"], ALL).kind == LEARNING
    assert cw.assess(frames["black"], ALL).kind == COVERED


def test_one_event_when_a_problem_starts_and_one_when_it_ends(frames):
    cw = learned_watch(frames)
    out = feed(cw, [frames["black"]] * 10 + [frames["normal"]] * 10)
    assert [t.label for t in out] == ["tamper:covered", "tamper:ok"]
    assert out[0].payload["previous"] == OK
    assert out[1].payload["previous"] == COVERED
    assert out[1].payload["previous_lasted_sec"] == 10 * 30  # confirmed on sample 3, cleared on sample 13


def test_a_problem_needs_three_samples_in_a_row(frames):
    cw = learned_watch(frames)
    assert feed(cw, [frames["black"], frames["black"], frames["normal"]] * 4) == []
    assert feed(cw, [frames["black"]] * 3)[0].label == "tamper:covered"


def test_flapping_does_not_post(frames):
    cw = learned_watch(frames)
    assert feed(cw, [frames["black"], frames["normal"]] * 10) == []
    assert cw.state == OK


def test_recovery_needs_three_good_samples(frames):
    cw = learned_watch(frames)
    feed(cw, [frames["black"]] * 3)
    assert feed(cw, [frames["normal"], frames["normal"], frames["black"]] * 3) == []
    assert cw.state == COVERED


def test_custom_confirm(frames):
    cw = learned_watch(frames, confirm=1)
    assert [t.label for t in feed(cw, [frames["black"], frames["normal"]])] == ["tamper:covered", "tamper:ok"]


def test_payload_explains_the_verdict(frames):
    cw = learned_watch(frames)
    p = feed(cw, [frames["blurred"]] * 3)[0].payload
    assert p["state"] == "blurred"
    assert p["measured"]["sharpness_ratio"] < p["thresholds"]["blur_ratio"]
    assert p["confirmed_after_samples"] == 3
    p = feed(cw, [frames["normal"]] * 3 + [frames["moved"]] * 3)[-1].payload
    assert p["state"] == "moved" and p["previous"] == "ok"
    assert p["measured"]["shift_percent"]["x"] < -5


def test_one_problem_to_another(frames):
    cw = learned_watch(frames)
    out = feed(cw, [frames["black"]] * 3 + [frames["moved"]] * 3 + [frames["normal"]] * 3)
    assert [t.label for t in out] == ["tamper:covered", "tamper:moved", "tamper:ok"]


def test_activity_does_not_post(frames):
    cw = learned_watch(frames)
    assert feed(cw, [frames["busy"], frames["normal"], frames["busy"], frames["busy"], frames["busy"]] * 4) == []


def test_moved_switched_off(frames):
    cw = learned_watch(frames)
    assert feed(cw, [frames["moved"]] * 6, checks=Checks(moved=False)) == []


def test_covered_switched_off_gives_no_verdict_for_a_black_picture(frames):
    cw = learned_watch(frames)
    assert feed(cw, [frames["black"]] * 6, checks=Checks(covered=False)) == []


def test_night_vision_is_learned_separately(frames):
    cw = learned_watch(frames)
    grey = [frames["grey"]] * 10
    assert feed(cw, grey) == []                       # learning the night view: no verdict
    assert not cw.learning("grey")
    assert [t.label for t in feed(cw, [frames["grey_moved"]] * 3)] == ["tamper:moved"]


def test_no_learning_while_a_problem_is_open(frames):
    cw = learned_watch(frames)
    feed(cw, [frames["moved"]] * 3)
    assert feed(cw, [frames["grey_moved"]] * 20) == []  # night falls on a moved camera: it does not learn that view
    assert cw.learning("grey") and cw.state == MOVED


def test_relearn_closes_an_open_problem(frames):
    cw = learned_watch(frames)
    feed(cw, [frames["moved"]] * 3)
    t = cw.relearn("2026-11-24")
    assert t.label == "tamper:ok" and t.payload["previous"] == MOVED and "learn" in t.payload["reason"]
    assert cw.learning("colour") and cw.relearn_token == "2026-11-24"


def test_a_new_picture_size_learns_again(frames, w):
    cw = learned_watch(frames)
    small = measure(S.add_noise(S.view(w), 3)[::2, ::2].copy(), TH)
    assert cw.observe(small, ALL) is None
    assert cw.learning("colour")


def test_saved_and_loaded(frames, tmp_path):
    cw = learned_watch(frames)
    feed(cw, [frames["black"]] * 3)
    path = tmp_path / "camera-7.npz"
    cw.save(path)
    again = CameraWatch(7, TH)
    assert again.load(path)
    assert again.state == COVERED and not again.learning("colour")
    assert abs(again.profiles["colour"].sharpness - cw.profiles["colour"].sharpness) < 1e-9
    # no repeated alarm after a restart, and the recovery is still reported
    assert [t.label for t in feed(again, [frames["black"]] * 5 + [frames["normal"]] * 3)] == ["tamper:ok"]


def test_a_broken_file_is_ignored(tmp_path):
    p = tmp_path / "camera-7.npz"
    p.write_bytes(b"not a file")
    assert not CameraWatch(7, TH).load(p)
    assert not CameraWatch(7, TH).load(tmp_path / "missing.npz")


# ---- confidence --------------------------------------------------------------------------------------------------

def test_confidence_is_half_at_the_threshold_and_grows_with_the_margin():
    from tamper_watch.detect import Thresholds
    from tamper_watch.watch import confidence
    th = Thresholds()
    assert confidence("covered", {"brightness_spread": 20.0, "brightness_p95": 120.0}, th) == 0.5
    assert confidence("covered", {"brightness_spread": 10.0, "brightness_p95": 120.0}, th) == pytest.approx(0.745, abs=0.006)
    assert confidence("covered", {"brightness_spread": 30.0, "brightness_p95": 0.0}, th) == 0.99   # black: p95 decides
    assert confidence("blurred", {"sharpness_ratio": 0.6}, th) == 0.5
    assert confidence("blurred", {"sharpness_ratio": 0.3}, th) == pytest.approx(0.745, abs=0.006)
    assert confidence("blurred", {"sharpness_ratio": 0.0}, th) == 0.99
    assert confidence("moved", {"areas_in_place": 0.2, "middle_best_shifted": 0.5}, th) == pytest.approx(0.745, abs=0.006)
    assert confidence("moved", {"areas_in_place": 0.0}, th) == 0.99
    # found again shifted while most areas look in place: the shifted match decides
    assert confidence("moved", {"areas_in_place": 0.9, "middle_best_shifted": 0.9}, th) == pytest.approx(0.745, abs=0.006)
    # missing numbers never give more than the floor
    assert confidence("moved", {"areas_in_place": None}, th) == 0.5
    for v in (0.0, 0.1, 0.39, 0.4):
        assert 0.5 <= confidence("moved", {"areas_in_place": v}, th) <= 0.99


def test_a_confirmed_problem_carries_its_confidence_and_recovery_none(frames):
    cw = learned_watch(frames)
    clock = Clock()
    out = feed(cw, [frames["black"]] * 3 + [frames["blurred"]] * 3 + [frames["moved"]] * 3 + [frames["normal"]] * 3,
               clock=clock)
    assert [t.label for t in out] == ["tamper:covered", "tamper:blurred", "tamper:moved", "tamper:ok"]
    for t in out[:3]:
        assert 0.5 <= t.confidence <= 0.99 and t.payload["confidence"] == t.confidence
    assert out[-1].confidence is None and "confidence" not in out[-1].payload
    assert out[-1].at == clock.t
