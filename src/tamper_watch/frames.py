"""One current frame of a camera from Vigiora Server: GET /api/hal/frame?monitor_id=N&width=W (Server 1.7.4, scope
streams:read, assigned cameras only). The server reads the camera's stream itself, so the add-on needs no network path
to the cameras or to the server's RTSP listener.

Answers: 200 image/jpeg (header X-Vigiora-Frame-At); 403 not assigned or no scope; 404 unknown camera; 503 with
Retry-After when no frame came within about 5 s (camera offline); 429 with Retry-After over the limit (2 frames/s per
camera, burst 5, 4 in flight per add-on). Errors are JSON {"error": ...}.

Kit gap: vigiora-addon-kit 0.2.0 has no public way to GET bytes (its request methods parse JSON), so this sends the
request itself with the client's address, token and TLS setting. It makes one attempt: the next try is the next round.
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

from vigiora_addon import Client, Unauthorized

#: The width asked for. The server may send another size; detect.measure resizes to its own working sizes anyway.
FRAME_WIDTH = 640
USER_AGENT = "vigiora-addon-tamper-watch"


@dataclass
class FrameResult:
    frame: np.ndarray | None
    status: int                  # HTTP status; 0 when the server could not be reached
    why: str = ""                # why there is no frame
    retry_after: float = 0.0     # seconds, from Retry-After on 429 and 503
    at: str = ""                 # X-Vigiora-Frame-At

    @property
    def unreachable(self) -> bool:
        """The camera gave no picture (offline, or the server cannot read it): no verdict."""
        return self.frame is None and self.status != 429


def _retry_after(headers) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0.0


def _error(raw: bytes) -> str:
    try:
        d = json.loads(raw)
        return str(d.get("error") or "error") if isinstance(d, dict) else "error"
    except ValueError:
        return raw[:200].decode("utf-8", "replace") or "error"


def fetch_frame(client: Client, camera_id: int, timeout: float = 10.0, width: int = FRAME_WIDTH) -> FrameResult:
    """One BGR frame of the camera, through the server. Raises Unauthorized on 401 (the token is gone)."""
    url = f"{client.settings.url}/api/hal/frame?monitor_id={int(camera_id)}&width={int(width)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "image/jpeg, application/json", "Authorization": f"Bearer {client.token}"}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=client._ssl) as r:  # noqa: SLF001 - see the kit gap above
            status, raw, h = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, h = e.code, e.read(), e.headers
    except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError) as e:
        return FrameResult(None, 0, f"cannot reach the server ({getattr(e, 'reason', e)})")
    if status == 401:
        raise Unauthorized(status, _error(raw))
    if status != 200:
        return FrameResult(None, status, f"{status}: {_error(raw)}", _retry_after(h))
    frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
    if frame is None or not frame.size:
        return FrameResult(None, status, "the server sent a picture that cannot be decoded")
    return FrameResult(frame, status, at=h.get("X-Vigiora-Frame-At") or "")
