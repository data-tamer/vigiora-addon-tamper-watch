"""Settings. The defaults are written out in compose.yaml (environment). A service-wide setting of the same name on the
server wins over the environment, and a camera's own settings (covered, blurred, moved, relearn) win for that camera.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from .detect import Thresholds
from .watch import Checks

log = logging.getLogger("tamper_watch")

ON = ("1", "on", "true", "yes")
OFF = ("0", "off", "false", "no")

#: name -> (default, minimum, maximum) for the numbers.
NUMBERS: dict[str, tuple[float, float, float]] = {
    "TAMPER_INTERVAL_SEC": (30, 5, 3600),
    "TAMPER_CONFIRM_SAMPLES": (3, 1, 20),
    "TAMPER_LEARN_SAMPLES": (10, 3, 200),
    "TAMPER_ADAPT_RATE": (0.02, 0.0, 0.5),
    "TAMPER_FLAT_SPREAD": (20, 0, 255),
    "TAMPER_DARK_LEVEL": (16, 0, 255),
    "TAMPER_BLUR_RATIO": (0.6, 0.0, 1.0),
    "TAMPER_MOVED_MATCH": (0.4, 0.0, 1.0),
    "TAMPER_GRAB_TIMEOUT_SEC": (10, 2, 60),
    "TAMPER_CAMERA_INFO_SEC": (600, 30, 86400),
    "TAMPER_WORKERS": (2, 1, 8),
}
SWITCHES = {"TAMPER_COVERED": True, "TAMPER_BLURRED": True, "TAMPER_MOVED": True}
#: Tags that mark a camera that moves by design. A camera with one of these gets the "moved" test off unless its own
#: setting says moved: on. Vigiora Server (1.7.4 included) does not tell add-ons whether a camera is PTZ; a tag is the way.
PTZ_TAGS = ("ptz", "patrol")


def switch(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    t = v.strip().lower()
    if t in ON:
        return True
    if t in OFF:
        return False
    return default  # "auto", empty or unknown


@dataclass
class Config:
    interval: float = 30
    confirm: int = 3
    learn: int = 10
    adapt: float = 0.02
    grab_timeout: float = 10
    camera_info: float = 600
    workers: int = 2
    rtsp_fallback: bool = False
    th: Thresholds = field(default_factory=Thresholds)
    checks: Checks = field(default_factory=Checks)

    @classmethod
    def load(cls, env: Mapping[str, str], server: Mapping[str, str] | None = None) -> "Config":
        """Environment values, then the server's service-wide settings of the same name on top."""
        merged = {k: v for k, v in env.items() if k.startswith("TAMPER_")}
        for k, v in (server or {}).items():  # the server may name them TAMPER_BLUR_RATIO or blur_ratio
            name = k if k.startswith("TAMPER_") else "TAMPER_" + k.upper()
            if name in NUMBERS or name in SWITCHES or name == "TAMPER_RTSP_FALLBACK":
                merged[name] = v
        n: dict[str, float] = {}
        for name, (default, lo, hi) in NUMBERS.items():
            raw = merged.get(name)
            if raw is None or raw.strip() == "":
                n[name] = default
                continue
            try:
                n[name] = min(hi, max(lo, float(raw)))
            except ValueError:
                log.warning("%s=%r is not a number; using %s", name, raw, default)
                n[name] = default
        return cls(
            interval=n["TAMPER_INTERVAL_SEC"], confirm=int(n["TAMPER_CONFIRM_SAMPLES"]), learn=int(n["TAMPER_LEARN_SAMPLES"]),
            adapt=n["TAMPER_ADAPT_RATE"], grab_timeout=n["TAMPER_GRAB_TIMEOUT_SEC"], workers=int(n["TAMPER_WORKERS"]),
            camera_info=n["TAMPER_CAMERA_INFO_SEC"], rtsp_fallback=switch(merged.get("TAMPER_RTSP_FALLBACK"), False),
            th=Thresholds(flat_spread=n["TAMPER_FLAT_SPREAD"], dark_level=n["TAMPER_DARK_LEVEL"],
                          blur_ratio=n["TAMPER_BLUR_RATIO"], moved_match=n["TAMPER_MOVED_MATCH"]),
            checks=Checks(**{k[len("TAMPER_"):].lower(): switch(merged.get(k), d) for k, d in SWITCHES.items()}),
        )

    def checks_for(self, camera_settings: Mapping[str, str], tags: list | None = None, has_ptz: bool = False) -> Checks:
        """The tests for one camera: the add-on's switches, "moved" off for a camera tagged ptz or patrol (or that the
        server reports as PTZ), and the camera's own covered / blurred / moved settings last."""
        moved = self.checks.moved
        if has_ptz or any(str(t).strip().lower() in PTZ_TAGS for t in tags or []):
            moved = False
        return Checks(
            covered=switch(camera_settings.get("covered"), self.checks.covered),
            blurred=switch(camera_settings.get("blurred"), self.checks.blurred),
            moved=switch(camera_settings.get("moved"), moved),
        )
