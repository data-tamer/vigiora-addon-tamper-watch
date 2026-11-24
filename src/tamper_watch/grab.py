"""Takes one frame from an RTSP address with OpenCV's own FFmpeg reader (inside the opencv-python-headless wheel, so
the image needs no system FFmpeg). The connection is opened, a few frames are read, and it is closed again: between
samples the add-on holds no stream and decodes nothing.
"""
from __future__ import annotations

import os

# RTSP over TCP: go2rtc serves it, and it survives Docker networks and packet loss better than UDP. A short probe
# (1 s, 1 MB: enough for a large keyframe) halves the time to the first frame. Read when a capture is opened.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|analyzeduration;1000000|probesize;1000000")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

#: Frames read per sample; the last one is used. The first frames after connecting can be half decoded.
FRAMES_PER_SAMPLE = 4


def grab_frame(url: str, timeout_sec: float = 10.0) -> np.ndarray | None:
    """One BGR frame, or None when the address cannot be opened or sends nothing in time."""
    ms = int(timeout_sec * 1000)
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms])
    try:
        if not cap.isOpened():
            return None
        frame = None
        for _ in range(FRAMES_PER_SAMPLE):
            ok, f = cap.read()
            if not ok:
                break
            frame = f
        return frame
    finally:
        cap.release()
