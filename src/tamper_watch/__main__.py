"""Tamper Watch: for every camera assigned to it, gets one frame from the server every N seconds and posts
tamper:covered, tamper:blurred or tamper:moved when such a problem starts (after it was seen on a few samples in a
row), and tamper:ok when the picture is normal again.

Run it:  tamper-watch   (or: python -m tamper_watch). Settings come from the environment, see docs/how-to.md.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from vigiora_addon import AddonError, Client, Forbidden, NotFound, Transient, Unauthorized
from vigiora_addon.events import ASSIGNMENT_CHANGED, CONFIG_UPDATED, TICK

from .config import Config
from .detect import measure
from .frames import FrameResult, fetch_frame
from .watch import OK, CameraWatch, Transition

ADDON_ID = "tamper-watch"  # the same as "id" in vigiora-addon.yaml
log = logging.getLogger("tamper_watch")
#: Learned views are written at least this often (in samples) while they adapt, and at once when the state changes.
SAVE_EVERY = 20
#: What Vigiora Server before 1.7.4 answers to a detection from an add-on.
NO_DETECTIONS = "this service does not report detections"
#: Events kept for a later try when the server cannot take them.
OUTBOX_MAX = 200

Grabber = Callable[[str, float], "np.ndarray | None"]
FrameSource = Callable[[Client, int, float], FrameResult]


def _rtsp_grabber(url: str, timeout: float):
    from .grab import grab_frame  # only with TAMPER_RTSP_FALLBACK on
    return grab_frame(url, timeout)


@dataclass
class Sample:
    camera_id: int
    frame: np.ndarray | None
    url: str = ""                # where the frame came from: "server" or an RTSP address
    why: str = ""                # why there is no frame
    skipped: bool = False        # not asked this round (the server said to wait): no change of any kind
    tags: list | None = None
    has_ptz: bool = False


class TamperWatch:
    def __init__(self, client: Client, env: dict[str, str], data_dir: str | Path, grab: Grabber = _rtsp_grabber,
                 clock: Callable[[], float] = time.time, frames: FrameSource = fetch_frame):
        self.client = client
        self.env = env
        self.grab = grab
        self.frames = frames
        self.clock = clock
        self.dir = Path(data_dir) / "tamper-watch"
        self.cfg = Config.load(env)
        self.cameras: list[int] = []
        self.camera_settings: dict[int, dict[str, str]] = {}
        self.running = True
        self.watches: dict[int, CameraWatch] = {}
        self.reachable: dict[int, bool] = {}
        self.url_ok: dict[int, str] = {}       # the RTSP address that worked last time (fallback only)
        self.info: dict[int, tuple[float, object]] = {}   # camera -> (when read, its stream config)
        self.not_before: dict[int, float] = {}            # camera -> do not ask for a frame before (Retry-After)
        self.outbox: list[tuple[int, Transition]] = []
        #: False once the server said it takes no detections from add-ons (Vigiora Server before 1.7.4): problems
        #: are then posted as plain results for the rest of the run.
        self.detections = True
        self.rounds = 0

    # ---- events from the server ---------------------------------------------------

    def handle(self, ev) -> None:
        if ev.type == ASSIGNMENT_CHANGED:
            self.cameras = ev.data["monitors"]
            self.info.clear()        # tags may have changed with the assignment: read them again
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

    def camera_info(self, cam: int):
        """The camera's stream config (tags, enabled, RTSP addresses), read again every TAMPER_CAMERA_INFO_SEC."""
        now = time.monotonic()
        got = self.info.get(cam)
        if got is None or now - got[0] >= self.cfg.camera_info:
            got = (now, self.client.stream_config(cam))
            self.info[cam] = got
        return got[1]

    def fetch(self, cam: int) -> Sample:
        """Runs in a worker thread: one frame of the camera, from the server (GET /api/hal/frame)."""
        try:
            sc = self.camera_info(cam)
        except (NotFound, Forbidden) as e:
            return Sample(cam, None, why=f"the server does not give this camera ({e})")
        except AddonError as e:
            return Sample(cam, None, why=f"cannot ask the server about this camera ({e})")
        tags, has_ptz = list(sc.tags), bool(sc.raw.get("has_ptz") or sc.raw.get("ptz"))
        if not sc.enabled:
            return Sample(cam, None, why="the camera is disabled", tags=tags, has_ptz=has_ptz)
        now = time.monotonic()
        if self.not_before.get(cam, 0.0) > now:
            return Sample(cam, None, why="the server asked to wait", skipped=True)
        r = self.frames(self.client, cam, self.cfg.grab_timeout)
        if r.frame is not None:
            return Sample(cam, r.frame, url="server", tags=tags, has_ptz=has_ptz)
        if r.status == 429:
            # Over the server's frame limit: not a verdict on the camera. Retry-After is honoured; a 503's Retry-After
            # (10 s) is shorter than a round, so it needs no wait of its own.
            self.not_before[cam] = now + max(r.retry_after, 1.0)
            log.debug("camera %s: frame limit reached; asking again in %.0f s", cam, max(r.retry_after, 1.0))
            return Sample(cam, None, why=r.why, skipped=True)
        if self.cfg.rtsp_fallback and r.status != 403:
            urls = sc.rtsp_urls(host=self.client.settings.rtsp_host)
            if self.url_ok.get(cam) in urls:
                urls.remove(self.url_ok[cam])
                urls.insert(0, self.url_ok[cam])
            for url in urls:
                frame = self.grab(url, self.cfg.grab_timeout)
                if frame is not None and frame.size:
                    return Sample(cam, frame, url=url, tags=tags, has_ptz=has_ptz)
            return Sample(cam, None, why=f"no frame from the server ({r.why}) nor from RTSP", tags=tags, has_ptz=has_ptz)
        return Sample(cam, None, why=f"no frame from the server ({r.why})", tags=tags, has_ptz=has_ptz)

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
        if s.skipped:
            return
        if s.frame is None:
            # No frame is not a verdict: an unreachable camera is not "covered". Nothing is posted.
            if self.reachable.get(cam, True):
                log.warning("camera %s: no picture (%s); no verdict until it answers", cam, s.why)
            self.reachable[cam] = False
            return
        if not self.reachable.get(cam, True):
            log.info("camera %s: picture back", cam)
        self.reachable[cam] = True
        if s.url != "server":
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

    def body(self, cam: int, t: Transition) -> dict:
        """What is posted for a change. covered, blurred and moved are detections, which the camera's alert rule for
        that label turns into alerts; tamper:ok (and every change on a server that takes no detections from add-ons)
        is a plain result, kept with the camera but never an alert."""
        b = {"monitor_id": cam, "label": t.label, "payload": t.payload}
        if t.confidence is not None and self.detections:
            at = datetime.fromtimestamp(t.at, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            b["detection"] = {"confidence": t.confidence, "detected_at": at}
        return b

    def flush(self) -> None:
        """Posts the outbox in order. A 429 or a server that cannot be reached leaves the change at the head of the
        outbox for the next round (the kit has already retried with backoff)."""
        while self.outbox:
            cam, t = self.outbox[0]
            body = self.body(cam, t)
            try:
                self.client.post_event(t.label, monitor_id=cam, payload=t.payload, detection=body.get("detection"))
            except Unauthorized:
                raise
            except AddonError as e:
                if "detection" in body and e.status == 400 and NO_DETECTIONS in e.message:
                    self.detections = False
                    log.warning("this Vigiora Server takes no detections from add-ons (it is older than 1.7.4): "
                                "tamper problems are posted as results only and raise no alert; update the server")
                    continue
                if isinstance(e, Transient):
                    log.warning("cannot post %s for camera %s yet (%s); trying again next round", t.label, cam, e)
                    return
                if isinstance(e, Forbidden):
                    log.warning("not posting %s for camera %s: it is not assigned to me any more", t.label, cam)
                else:
                    log.error("the server refused %s for camera %s (%s); dropped", t.label, cam, e)
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
