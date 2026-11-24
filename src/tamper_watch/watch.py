"""One camera: its learned normal view, its current state, and when to post.

CameraWatch.observe(measure) takes one sample and returns a Transition when the camera's state changes:
one event when a problem starts (after it was seen on `confirm` samples in a row), one when it ends.

The normal view is learned per picture mode ("colour" by day, "grey" with night vision), over the first `learn`
samples of that mode, and then adapted slowly while the state is ok. It is never learned or adapted while a problem
is open, so a covered or turned camera is not slowly accepted as normal.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .detect import Measure, Thresholds, edge_match, is_covered, is_moved

log = logging.getLogger("tamper_watch")

OK, COVERED, BLURRED, MOVED = "ok", "covered", "blurred", "moved"
#: Verdicts that are not states: the current mode is still being learned, or the picture cannot be judged.
LEARNING, NONE = "learning", "none"
STATES = (OK, COVERED, BLURRED, MOVED)
FORMAT = 1


@dataclass
class Checks:
    covered: bool = True
    blurred: bool = True
    moved: bool = True

    def as_dict(self) -> dict:
        return {"covered": self.covered, "blurred": self.blurred, "moved": self.moved}


@dataclass
class Profile:
    """The learned normal view for one picture mode."""

    reference: np.ndarray | None = None      # float32, share of samples in which each pixel was an edge
    sharpness: float = 0.0
    size: tuple[int, int] = (0, 0)           # the frame size it was learned on
    edge_sum: np.ndarray | None = None       # while learning
    sharp_values: list[float] = field(default_factory=list)

    @property
    def learned(self) -> bool:
        return self.reference is not None

    @property
    def samples(self) -> int:
        return len(self.sharp_values)


@dataclass(frozen=True)
class Verdict:
    kind: str
    numbers: dict


@dataclass(frozen=True)
class Transition:
    label: str
    payload: dict


class CameraWatch:
    def __init__(self, camera_id: int, th: Thresholds, *, confirm: int = 3, learn: int = 10, adapt: float = 0.02):
        self.camera_id = camera_id
        self.th = th
        self.confirm = max(1, confirm)
        self.learn = max(1, learn)
        self.adapt = adapt
        self.state = OK
        self.since = time.time()
        self.pending: str | None = None
        self.pending_count = 0
        self.profiles: dict[str, Profile] = {}
        self.relearn_token = ""
        self.dirty = False

    # ---- judging one sample ---------------------------------------------------

    def learning(self, mode: str) -> bool:
        p = self.profiles.get(mode)
        return p is None or not p.learned

    def assess(self, m: Measure, checks: Checks) -> Verdict:
        n: dict = {"mode": m.mode, "brightness_spread": round(m.spread, 1), "brightness_p95": round(m.p95, 1)}
        if is_covered(m, self.th):
            # A flat or black picture cannot be judged for blur or a move; with the covered test off it gives no verdict.
            return Verdict(COVERED if checks.covered else NONE, n)
        prof = self.profiles.get(m.mode)
        if prof is None or not prof.learned:
            n["learning_samples"] = f"{prof.samples if prof else 0}/{self.learn}"
            return Verdict(LEARNING, n)
        ratio = m.sharpness / prof.sharpness if prof.sharpness > 0 else None
        match = edge_match(prof.reference, m.edges)
        n["sharpness"] = round(m.sharpness, 4)
        n["sharpness_normal"] = round(prof.sharpness, 4)
        n["sharpness_ratio"] = None if ratio is None else round(ratio, 3)
        if match is None:
            n["areas_in_place"] = None  # the view has too little detail to judge a move
        else:
            n["areas_in_place"] = round(match.kept, 3)
            n["areas_judged"] = match.areas
            n["edges_in_place"] = round(match.in_place, 3)
            n["middle_in_place"] = round(match.centre, 3)
            n["middle_best_shifted"] = round(match.shifted, 3)
            n["shift_percent"] = {"x": match.shift_x, "y": match.shift_y}
        if checks.blurred and ratio is not None and ratio < self.th.blur_ratio:
            return Verdict(BLURRED, n)
        if checks.moved and match is not None and is_moved(match, self.th):
            return Verdict(MOVED, n)
        return Verdict(OK, n)

    # ---- learning ---------------------------------------------------------------

    def _learn(self, m: Measure) -> None:
        prof = self.profiles.setdefault(m.mode, Profile())
        if prof.edge_sum is not None and prof.size != m.size:
            prof.edge_sum, prof.sharp_values = None, []
        prof.size = m.size
        prof.edge_sum = m.edges.astype(np.float32) if prof.edge_sum is None else prof.edge_sum + m.edges
        prof.sharp_values.append(m.sharpness)
        if prof.samples >= self.learn:
            prof.reference = prof.edge_sum / prof.samples
            prof.sharpness = float(np.median(prof.sharp_values))
            prof.edge_sum = None
            self.dirty = True
            log.info("camera %s: learned its %s view (sharpness %.4f)", self.camera_id, m.mode, prof.sharpness)

    def _adapt(self, m: Measure) -> None:
        prof = self.profiles[m.mode]
        a = self.adapt
        prof.reference = (1 - a) * prof.reference + a * m.edges
        prof.sharpness = (1 - a) * prof.sharpness + a * m.sharpness

    def relearn(self, token: str = "", reason: str = "asked to learn the view again",
                now: float | None = None) -> Transition | None:
        """Forgets the learned views (the camera was moved on purpose, or a new view is the normal one) and starts
        learning again. When a problem was open, it is closed with a tamper:ok event that says why."""
        now = time.time() if now is None else now
        prev, prev_since = self.state, self.since
        self.profiles = {}
        self.relearn_token = token
        self.state, self.since, self.pending, self.pending_count = OK, now, None, 0
        self.dirty = True
        log.info("camera %s: learning the view again (%s)", self.camera_id, reason)
        if prev == OK:
            return None
        return Transition("tamper:ok", {"state": OK, "previous": prev, "reason": reason,
                                        "previous_lasted_sec": round(now - prev_since)})

    # ---- the state machine ----------------------------------------------------

    def observe(self, m: Measure, checks: Checks, now: float | None = None) -> Transition | None:
        now = time.time() if now is None else now
        prof = self.profiles.get(m.mode)
        if prof is not None and prof.learned and prof.size != m.size:
            # The camera's resolution changed, or its low-resolution stream is gone and the main one is read:
            # sharpness and edges are not comparable any more, so every view is learned again.
            return self.relearn(self.relearn_token, f"the picture size changed from {prof.size[0]}x{prof.size[1]} "
                                f"to {m.size[0]}x{m.size[1]}", now)
        v = self.assess(m, checks)
        if self.state == OK and v.kind == LEARNING:
            self._learn(m)
        elif self.state == OK and v.kind == OK and self.pending is None:
            self._adapt(m)
        return self._step(v, m, checks, now)

    def _step(self, v: Verdict, m: Measure, checks: Checks, now: float) -> Transition | None:
        if v.kind == NONE:
            return None
        cand = v.kind
        if cand == LEARNING:
            if self.state not in (OK, COVERED):
                return None  # a blurred or moved camera in a mode not learned yet: no information
            cand = OK
        if cand == self.state:
            self.pending, self.pending_count = None, 0
            return None
        if cand == self.pending:
            self.pending_count += 1
        else:
            self.pending, self.pending_count = cand, 1
        if self.pending_count < self.confirm:
            return None
        prev, prev_since = self.state, self.since
        self.state, self.since, self.pending, self.pending_count = cand, now, None, 0
        self.dirty = True
        payload = {
            "state": cand,
            "previous": prev,
            "measured": v.numbers,
            "thresholds": self.th.as_dict(),
            "checks": checks.as_dict(),
            "confirmed_after_samples": self.confirm,
        }
        if prev != OK:
            payload["previous_lasted_sec"] = round(now - prev_since)
        log.info("camera %s: %s -> %s %s", self.camera_id, prev, cand, json.dumps(v.numbers))
        return Transition(f"tamper:{cand}", payload)

    # ---- saving -------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Writes the learned views and the state (atomic replace). Learning in progress is not kept."""
        arrays = {f"ref_{mode}": np.round(p.reference * 255).astype(np.uint8)
                  for mode, p in self.profiles.items() if p.learned}
        meta = {"format": FORMAT, "camera_id": self.camera_id, "state": self.state, "since": self.since,
                "relearn_token": self.relearn_token,
                "sharpness": {mode: p.sharpness for mode, p in self.profiles.items() if p.learned},
                "size": {mode: list(p.size) for mode, p in self.profiles.items() if p.learned}}
        buf = io.BytesIO()
        np.savez_compressed(buf, meta=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8), **arrays)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(buf.getvalue())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.dirty = False

    def load(self, path: Path) -> bool:
        """Reads what save() wrote. False (and nothing changed) when the file is missing or unreadable."""
        try:
            with np.load(path) as z:
                meta = json.loads(bytes(z["meta"]).decode())
                refs = {k[4:]: z[k].astype(np.float32) / 255.0 for k in z.files if k.startswith("ref_")}
        except (OSError, ValueError, KeyError) as e:
            if path.exists():
                log.warning("camera %s: cannot read %s (%s); learning again", self.camera_id, path, e)
            return False
        if meta.get("format") != FORMAT or meta.get("state") not in STATES:
            return False
        self.state = meta["state"]
        self.since = float(meta.get("since") or time.time())
        self.relearn_token = str(meta.get("relearn_token") or "")
        self.profiles = {mode: Profile(reference=ref, sharpness=float(meta["sharpness"].get(mode, 0.0)),
                                       size=tuple(meta.get("size", {}).get(mode, (0, 0))))
                         for mode, ref in refs.items()}
        return True
