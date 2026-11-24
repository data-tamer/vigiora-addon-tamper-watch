"""The whole add-on against the kit's fake Vigiora server, with frames from a fake grabber instead of RTSP."""
import threading

import pytest
import scenes as S

from tamper_watch.__main__ import TamperWatch, run
from tamper_watch.config import Config

ENV = {"TAMPER_LEARN_SAMPLES": "4", "TAMPER_CONFIRM_SAMPLES": "2", "TAMPER_GRAB_TIMEOUT_SEC": "2"}


class Cameras:
    """What each camera "sends": a frame per camera id, or None for a camera that does not answer."""

    def __init__(self):
        w = S.world(3)
        self.normal = S.add_noise(S.view(w), 3)
        self.moved = S.view(w, dx=12)
        self.black = S.flat((0, 0, 0), 1)
        self.now = {1: self.normal, 2: self.normal}
        self.urls = []

    def grab(self, url, timeout):
        self.urls.append(url)
        cam = int(url.rsplit("/cam", 1)[1].split("_")[0])
        return self.now.get(cam)


@pytest.fixture
def cams():
    return Cameras()


def make_app(client, cams, tmp_path, env=ENV):
    return TamperWatch(client, dict(env), tmp_path / "data", grab=cams.grab)


@pytest.fixture
def running(addon_client, cams, tmp_path):
    app = make_app(addon_client, cams, tmp_path)
    t = threading.Thread(target=run, args=(addon_client, app), daemon=True)
    app.cfg.interval = 0.05
    t.start()
    yield app
    addon_client.stop()
    t.join(timeout=10)
    assert not t.is_alive()


def labels(fake, cam=None):
    return [e["body"]["label"] for e in fake.events if cam is None or e["body"]["monitor_id"] == cam]


def learned(app, *cams_):
    return all(app.watches.get(c) and not app.watches[c].learning("colour") for c in cams_)


def test_covered_then_ok_against_the_fake_server(fake_vigiora, running, cams):
    assert fake_vigiora.wait_for(lambda f: learned(running, 1, 2), timeout=10)
    cams.now[1] = cams.black
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:covered"], timeout=10)
    e = fake_vigiora.events[0]["body"]
    assert e["monitor_id"] == 1
    assert e["payload"]["measured"]["brightness_p95"] <= e["payload"]["thresholds"]["dark_level"]
    cams.now[1] = cams.normal
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:covered", "tamper:ok"], timeout=10)
    assert labels(fake_vigiora, 2) == []


def test_reads_the_low_resolution_stream_first(fake_vigiora, running, cams):
    assert fake_vigiora.wait_for(lambda f: len(cams.urls) >= 2, timeout=10)
    assert cams.urls[0].endswith("_sub")


def test_an_unreachable_camera_is_not_covered(fake_vigiora, running, cams):
    cams.now[2] = None
    assert fake_vigiora.wait_for(lambda f: any(
        m["body"].get("extra", {}).get("cameras", {}).get("2", {}).get("reachable") is False for m in f.metrics), timeout=10)
    fake_vigiora.wait_for(lambda f: False, timeout=0.5)
    assert labels(fake_vigiora, 2) == []


def test_a_ptz_tagged_camera_is_not_reported_moved(fake_vigiora, running, cams):
    fake_vigiora.add_camera(3, tags=["PTZ"])
    fake_vigiora.assign([1, 2, 3])
    cams.now[3] = cams.normal
    assert fake_vigiora.wait_for(lambda f: learned(running, 1, 3), timeout=10)
    cams.now[1] = cams.now[3] = cams.moved
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:moved"], timeout=10)
    fake_vigiora.wait_for(lambda f: False, timeout=0.5)
    assert labels(fake_vigiora, 3) == []


def test_a_camera_setting_turns_moved_off(fake_vigiora, running, cams):
    fake_vigiora.set_camera_config(2, moved="off")
    assert fake_vigiora.wait_for(lambda f: learned(running, 1, 2), timeout=10)
    cams.now[1] = cams.now[2] = cams.moved
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:moved"], timeout=10)
    fake_vigiora.wait_for(lambda f: False, timeout=0.5)
    assert labels(fake_vigiora, 2) == []


def test_relearn_setting_accepts_a_new_view(fake_vigiora, running, cams):
    assert fake_vigiora.wait_for(lambda f: learned(running, 1), timeout=10)
    cams.now[1] = cams.moved
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:moved"], timeout=10)
    fake_vigiora.set_camera_config(1, relearn="2026-11-24")
    assert fake_vigiora.wait_for(lambda f: labels(f, 1) == ["tamper:moved", "tamper:ok"], timeout=10)
    assert fake_vigiora.wait_for(lambda f: learned(running, 1), timeout=10)
    fake_vigiora.wait_for(lambda f: False, timeout=0.5)
    assert labels(fake_vigiora, 1) == ["tamper:moved", "tamper:ok"]


def test_a_server_setting_wins_over_the_environment(fake_vigiora, running, cams):
    fake_vigiora.set_config(moved="off", blur_ratio="0.4")
    assert fake_vigiora.wait_for(lambda f: running.cfg.th.blur_ratio == 0.4 and not running.cfg.checks.moved, timeout=10)


def test_stopped_takes_no_frames(fake_vigiora, running, cams):
    fake_vigiora.set_desired_state("stopped")
    assert fake_vigiora.wait_for(lambda f: not running.running, timeout=10)
    fake_vigiora.wait_for(lambda f: False, timeout=0.3)
    n = len(cams.urls)
    fake_vigiora.wait_for(lambda f: False, timeout=0.5)
    assert len(cams.urls) == n


def test_events_wait_when_the_server_is_down(fake_vigiora, addon_client, cams, tmp_path):
    app = make_app(addon_client, cams, tmp_path)
    app.cameras = [1]
    for _ in range(4):
        app.sample_all()
    cams.now[1] = cams.black
    fake_vigiora.fail_next(3, 503, "/api/hal/events")  # one post: the client tries 3 times
    app.sample_all()
    app.sample_all()                     # confirmed here; the post fails and is kept
    assert labels(fake_vigiora) == [] and len(app.outbox) == 1
    app.sample_all()                     # posted on the next round
    assert labels(fake_vigiora) == ["tamper:covered"]


def test_a_restart_keeps_the_learned_view(fake_vigiora, addon_client, cams, tmp_path):
    app = make_app(addon_client, cams, tmp_path)
    app.cameras = [1]
    for _ in range(4):
        app.sample_all()
    assert learned(app, 1)
    app.save_all()
    again = make_app(addon_client, cams, tmp_path)
    again.cameras = [1]
    cams.now[1] = cams.moved
    again.sample_all()
    again.sample_all()
    assert labels(fake_vigiora) == ["tamper:moved"]   # judged at once: nothing to learn again


def test_config_defaults_match_compose():
    import yaml
    from pathlib import Path
    env = yaml.safe_load((Path(__file__).parent.parent / "compose.yaml").read_text())["services"]["tamper-watch"]["environment"]
    assert Config.load(env) == Config.load({})


# ---- the shape of what is posted ------------------------------------------------------------------------------
# Vigiora Server 1.7.x stores a result with a "detection" object in its detections table (which alert rules, the
# Alerts page and notifications read) only for its own detection services: internal/handlers/halstream/handler.go
# answers 400 "this service does not report detections" to any other service, add-ons included
# (halcatalog.DetectionServices). So Tamper Watch posts plain results, which the server keeps in hal_events.

def test_posted_body_is_a_plain_result(fake_vigiora, addon_client, cams, tmp_path):
    app = make_app(addon_client, cams, tmp_path)
    app.cameras = [1]
    for _ in range(4):
        app.sample_all()
    cams.now[1] = cams.black
    app.sample_all()
    app.sample_all()
    (e,) = fake_vigiora.events
    body = e["body"]
    assert set(body) == {"label", "monitor_id", "payload"}            # no "detection": the server would refuse it
    assert body["label"] == "tamper:covered" and body["monitor_id"] == 1
    p = body["payload"]
    assert p["state"] == "covered" and p["previous"] == "ok"
    assert set(p["measured"]) >= {"mode", "brightness_spread", "brightness_p95"}
    assert p["thresholds"] == {"flat_spread": 20.0, "dark_level": 16.0, "blur_ratio": 0.6, "moved_match": 0.4}
    assert p["confirmed_after_samples"] == 2 and p["interval_sec"] == 30.0


def test_an_addon_cannot_post_a_detection(fake_vigiora, addon_client):
    from vigiora_addon import AddonError
    with pytest.raises(AddonError) as e:
        addon_client.post_event("tamper:covered", monitor_id=1, payload={}, detection={"confidence": 1.0})
    assert e.value.status == 400 and "does not report detections" in str(e.value)
