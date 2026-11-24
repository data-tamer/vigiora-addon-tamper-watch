"""Tamper Watch: for every camera assigned to it, takes one frame of the low-resolution stream every N seconds and posts
tamper:covered, tamper:blurred or tamper:moved when such a problem starts (after it was seen on a few samples in a
row), and tamper:ok when the picture is normal again.

Run it:  tamper-watch   (or: python -m tamper_watch). Settings come from the environment, see docs/how-to.md.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from vigiora_addon import AddonError, Client, Forbidden, NotFound, Transient, Unauthorized
from vigiora_addon.events import ASSIGNMENT_CHANGED, CONFIG_UPDATED, TICK

from .config import Config
from .detect import measure
from .watch import OK, CameraWatch, Transition

ADDON_ID = "tamper-watch"  # the same as "id" in vigiora-addon.yaml
log = logging.getLogger("tamper_watch")
#: Learned views are written at least this often (in samples) while they adapt, and at once when the state changes.
SAVE_EVERY = 20
#: Events kept for a later try when the server cannot take them.
OUTBOX_MAX = 200

Grabber = Callable[[str, float], "np.ndarray | None"]


def _default_grabber(url: str, timeout: float):
    from .grab import grab_frame  # imported here so the tests need no RTSP
    return grab_frame(url, timeout)


@dataclass
class Sample:
    camera_id: int
    frame: np.ndarray | None
    url: str = ""
    why: str = ""                # why there is no frame
    tags: list | None = None
    has_ptz: bool = False


class TamperWatch:
    def __init__(self, client: Client, env: dict[str, str], data_dir: str | Path, grab: Grabber = _default_grabber,
                 clock: Callable[[], float] = time.time):
        self.client = client
        self.env = env
        self.grab = grab
        self.clock = clock
        self.dir = Path(data_dir) / "tamper-watch"
        self.cfg = Config.load(env)
        self.cameras: list[int] = []
        self.camera_settings: dict[int, dict[str, str]] = {}
        self.running = True
        self.watches: dict[int, CameraWatch] = {}
        self.reachable: dict[int, bool] = {}
        self.url_ok: dict[int, str] = {}       # the address that worked last time
        self.outbox: list[tuple[int, Transition]] = []
        self.rounds = 0

    # ---- events from the server ---------------------------------------------------

    def handle(self, ev) -> None:
        if ev.type == ASSIGNMENT_CHANGED:
            self.cameras = ev.data["monitors"]
            for cam in ev.data["removed"]:
                w = self.watches.pop(cam, None)
                if w is not None and w.dirty:
                    self._save(w)  # kept on disk: assigning the camera again does not start learning again
            log.info("cameras assigned to me: %s", self.cameras)
        elif ev.type == CONFIG_UPDATED:
            server = ev.data["server"]
            self.cfg = Config.load(self.env, server["config"])
            self.running = server["desired_state"] != "stopped"
            self.camera_settings = {int(cam): dict(c) for cam, c in ev.data["cameras"].items()}
            for w in self.watches.values():
                w.th, w.confirm, w.learn, w.adapt = self.cfg.th, self.cfg.confirm, self.cfg.learn, self.cfg.adapt
            log.info("settings (%s): running=%s checks=%s thresholds=%s", ", ".join(ev.data["changed"]), self.running,
                     self.cfg.checks.as_dict(), self.cfg.th.as_dict())
        elif ev.type == TICK and self.running:
            self.sample_all()

    # ---- one round ------------------------------------------------------------------

    def watch_for(self, cam: int) -> CameraWatch:
        w = self.watches.get(cam)
        if w is None:
            w = CameraWatch(cam, self.cfg.th, confirm=self.cfg.confirm, learn=self.cfg.learn, adapt=self.cfg.adapt)
            if w.load(self._path(cam)):
                log.info("camera %s: loaded its learned views %s, state %s", cam, sorted(w.profiles), w.state)
            self.watches[cam] = w
        return w

    def fetch(self, cam: int) -> Sample:
        """Runs in a worker thread: the camera's addresses from the server, then one frame."""
        try:
            sc = self.client.stream_config(cam)
        except (NotFound, Forbidden) as e:
            return Sample(cam, None, why=f"the server does not give its stream ({e})")
        except AddonError as e:
            return Sample(cam, None, why=f"cannot ask the server for its stream ({e})")
        tags, has_ptz = list(sc.tags), bool(sc.raw.get("has_ptz") or sc.raw.get("ptz"))
        if not sc.enabled:
            return Sample(cam, None, why="the camera is disabled", tags=tags, has_ptz=has_ptz)
        urls = sc.rtsp_urls(host=self.client.settings.rtsp_host)
        if self.url_ok.get(cam) in urls:
            urls.remove(self.url_ok[cam])
            urls.insert(0, self.url_ok[cam])
        for url in urls:
            frame = self.grab(url, self.cfg.grab_timeout)
            if frame is not None and frame.size:
                return Sample(cam, frame, url=url, tags=tags, has_ptz=has_ptz)
        return Sample(cam, None, why="no frame from " + ", ".join(urls) if urls else "no address", tags=tags, has_ptz=has_ptz)

    def sample_all(self) -> None:
        self.rounds += 1
        cams = list(self.cameras)
        if cams:
            with ThreadPoolExecutor(max_workers=min(self.cfg.workers, len(cams))) as pool:
                samples = list(pool.map(self.fetch, cams))
        else:
            samples = []
        now = self.clock()
        for s in samples:
            try:
                self.judge(s, now)
            except Exception:  # one odd frame must not stop the other cameras
                log.exception("camera %s: could not judge the frame", s.camera_id)
        self.flush()
        for w in self.watches.values():
            if w.dirty or self.rounds % SAVE_EVERY == 0:
                self._save(w)
        try:
            self.client.heartbeat(extra={"cameras": {str(c): self.status(c) for c in cams}})
        except Unauthorized:
            raise
        except AddonError as e:
            log.warning("heartbeat not sent (%s)", e)

    def judge(self, s: Sample, now: float) -> None:
        cam = s.camera_id
        w = self.watch_for(cam)
        settings = self.camera_settings.get(cam, {})
        token = settings.get("relearn", "").strip()
        if token and token != w.relearn_token:
            self._queue(cam, w.relearn(token, "asked to learn the view again (camera setting relearn)", now))
        if s.frame is None:
            # No frame is not a verdict: an unreachable camera is not "covered". Nothing is posted.
            if self.reachable.get(cam, True):
                log.warning("camera %s: no picture (%s); no verdict until it answers", cam, s.why)
            self.reachable[cam] = False
            return
        if not self.reachable.get(cam, True):
            log.info("camera %s: picture back", cam)
        self.reachable[cam] = True
        self.url_ok[cam] = s.url
        checks = self.cfg.checks_for(settings, s.tags, s.has_ptz)
        self._queue(cam, w.observe(measure(s.frame, w.th), checks, now))

    def status(self, cam: int) -> dict:
        w = self.watches.get(cam)
        return {
            "state": w.state if w else OK,
            "learned": sorted(m for m, p in w.profiles.items() if p.learned) if w else [],
            "reachable": self.reachable.get(cam, False),
        }

    # ---- posting ----------------------------------------------------------------

    def _queue(self, cam: int, t: Transition | None) -> None:
        if t is not None:
            t.payload["interval_sec"] = self.cfg.interval
            self.outbox.append((cam, t))
            del self.outbox[:-OUTBOX_MAX]

    def flush(self) -> None:
        while self.outbox:
            cam, t = self.outbox[0]
            try:
                self.client.post_event(t.label, monitor_id=cam, payload=t.payload)
            except Unauthorized:
                raise
            except AddonError as e:
                log.warning("cannot post %s for camera %s yet (%s); trying again next round", t.label, cam, e)
                return
            self.outbox.pop(0)

    # ---- files --------------------------------------------------------------------

    def _path(self, cam: int) -> Path:
        return self.dir / f"camera-{cam}.npz"

    def _save(self, w: CameraWatch) -> None:
        try:
            w.save(self._path(w.camera_id))
        except OSError as e:
            log.warning("camera %s: cannot save its learned views (%s)", w.camera_id, e)

    def save_all(self) -> None:
        for w in self.watches.values():
            self._save(w)


def run(client: Client, app: TamperWatch) -> int:
    """Runs until client.stop() is called (the container's SIGTERM does that). Returns an exit code."""
    try:
        for ev in client.events(poll_interval=app.cfg.interval, tick=True):
            app.handle(ev)
    except Unauthorized:
        log.error("the server no longer accepts my token (revoked?). Stopping.")
        return 3
    finally:
        app.save_all()
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = Client.from_env(ADDON_ID)
    client.install_signal_handlers()
    log.info("server %s, data in %s", client.settings.url, client.settings.data_dir)
    while not client.stopping.is_set():  # the server may still be starting: keep trying
        try:
            client.ensure_enrolled()
            break
        except Transient as e:
            log.warning("cannot enrol yet: %s", e)
            client.sleep(10)
        except Unauthorized as e:
            log.error("enrolment refused: %s. Check the enrolment key.", e)
            return 2
    app = TamperWatch(client, dict(os.environ), client.settings.data_dir)
    log.info("sampling every %.0f s; checks %s; thresholds %s", app.cfg.interval, app.cfg.checks.as_dict(), app.cfg.th.as_dict())
    return run(client, app)


if __name__ == "__main__":
    sys.exit(main())
