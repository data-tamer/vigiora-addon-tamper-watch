"""The picture tests. Pure functions on numpy arrays: no network, no files, no clock.

A frame is an 8-bit image, H x W (grey) or H x W x 3 (BGR, as OpenCV decodes it).

    covered   the picture is almost one flat tone (spread of brightness small) or almost black
    blurred   the steepness of the strongest edges fell far below this camera's own normal
    moved     the lasting edges of the learned view are no longer where they were, or fit clearly better shifted

The tests use brightness ranks and ratios, so a darker or brighter picture, or the switch to night vision
(colour to grey), does not change them much. tools/false_alarm_check.py measures that.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

#: Width of the picture the edge map is computed on. Small on purpose: fast on a Raspberry Pi, and it ignores
#: leaves, rain drops and sensor noise.
EDGE_WIDTH = 320
#: Width of the picture sharpness is computed on (or the frame's own width when it is smaller).
SHARP_WIDTH = 640
#: The share of pixels, strongest gradient first, that count as edges in one picture (a rank, so lighting does not
#: move it).
EDGE_TOP_PERCENT = 12.0
#: A pixel of the learned view is a lasting edge when it was an edge in at least this share of the samples.
STABLE_EDGE = 0.6
#: Fewer lasting edge pixels than this share of the picture: the view is too plain to judge a move.
MIN_STABLE_SHARE = 0.01


@dataclass(frozen=True)
class Thresholds:
    flat_spread: float = 20.0     # covered when the 5th to 95th percentile of brightness (0-255) is this or less
    dark_level: float = 16.0      # covered when 95% of the pixels are this dark or darker (0-255)
    blur_ratio: float = 0.60      # blurred when sharpness is below this share of the camera's normal
    moved_match: float = 0.40     # moved when less than this share of the picture's areas is still in place
    grey_chroma: float = 4.0      # a picture whose mean colour saturation (0-255) is this or less is night/grey mode

    def as_dict(self) -> dict:
        return {"flat_spread": self.flat_spread, "dark_level": self.dark_level, "blur_ratio": self.blur_ratio,
                "moved_match": self.moved_match}


def _resize_to_width(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= width:
        return img
    return cv2.resize(img, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)


def to_grey(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    if frame.ndim == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    raise ValueError(f"not an image: shape {frame.shape}")


def chroma(frame: np.ndarray) -> float:
    """Mean colour saturation, 0 (grey) to 255: max(B,G,R) - min(B,G,R) averaged, on a small copy."""
    if frame.ndim == 2:
        return 0.0
    small = _resize_to_width(frame[:, :, :3], 160).astype(np.int16)
    return float((small.max(axis=2) - small.min(axis=2)).mean())


def mode_of(frame: np.ndarray, th: Thresholds) -> str:
    """"colour" or "grey" (night vision, or a camera that sends black and white)."""
    return "grey" if chroma(frame) <= th.grey_chroma else "colour"


def brightness_stats(grey: np.ndarray) -> tuple[float, float]:
    """(spread, p95): the 5th to 95th percentile range of brightness, and the 95th percentile. Percentiles, not the
    mean and deviation, so a time stamp burned into the picture does not hide a covered lens."""
    small = _resize_to_width(grey, EDGE_WIDTH)
    p5, p95 = np.percentile(small, (5, 95))
    return float(p95 - p5), float(p95)


def sharpness(grey: np.ndarray) -> float:
    """How steep the strongest edges are, relative to the picture's contrast: the 99.5th percentile of the gradient
    divided by the brightness spread (5th to 95th percentile). A darker or brighter picture changes both alike, and
    sensor noise hardly reaches the strongest edges, so it stays steady; a blur flattens every edge and it falls."""
    g = _resize_to_width(grey, SHARP_WIDTH).astype(np.float32)
    p5, p95 = np.percentile(g, (5, 95))
    spread = float(p95 - p5)
    if spread < 1.0:
        return 0.0
    mag = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    return float(np.percentile(mag, 99.5) / spread)


def edge_map(grey: np.ndarray) -> np.ndarray:
    """Boolean map, EDGE_WIDTH wide: the strongest EDGE_TOP_PERCENT of gradients after a light blur."""
    g = _resize_to_width(grey, EDGE_WIDTH).astype(np.float32)
    g = cv2.GaussianBlur(g, (0, 0), 1.2)
    mag = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    thr = float(np.percentile(mag, 100.0 - EDGE_TOP_PERCENT))
    if thr <= 1e-3:
        return np.zeros(mag.shape, dtype=bool)
    return mag >= thr


_DILATE = np.ones((3, 3), np.uint8)
#: The picture is judged in GRID[0] x GRID[1] areas. Someone close to the lens hides some areas; a moved camera
#: loses its edges in nearly all of them.
GRID = (4, 3)
#: An area is still in place when this share of its lasting edges is found again.
CELL_OK = 0.6
#: An area with fewer lasting edge pixels than this (sky, a wall) is not judged.
MIN_CELL_EDGES = 20
#: The shift search looks this far (share of the width and height) in each direction.
SEARCH = 0.15
#: A shift smaller than this share of the picture is not a move (wind, vibration, slow drift).
MIN_SHIFT = 0.025
#: A view found again this much better at a shifted position than in place has moved...
SHIFT_GAIN = 0.15
#: ... when it is found again at least this well there. A real pan or tilt finds 0.85 or more; someone close to the
#: lens can make a wrong shift look 0.65 to 0.75 good.
SHIFT_MATCH = 0.8


@dataclass(frozen=True)
class Match:
    in_place: float        # share of all lasting edges found again where they were (shown, not used to decide)
    kept: float            # share of the judged areas still in place
    areas: int             # areas judged
    centre: float          # share of the lasting edges of the middle of the picture found in place...
    shifted: float         # ... and at the best shift within SEARCH (the same as centre when no shift is better)
    shift_x: float         # that shift in percent of the width and height
    shift_y: float


def edge_match(reference: np.ndarray, edges: np.ndarray) -> Match | None:
    """How well the current edge map matches the reference's lasting edges (reference >= STABLE_EDGE), within one
    pixel: overall, per area, and at the best shift. None when the reference has too few lasting edges to judge
    (a plain view) or the sizes differ."""
    if reference.shape != edges.shape:
        return None
    stable = reference >= STABLE_EDGE
    total = int(stable.sum())
    if total < MIN_STABLE_SHARE * stable.size:
        return None
    near = cv2.dilate(edges.astype(np.uint8), _DILATE).astype(bool)
    hits = stable & near
    h, w = stable.shape
    areas = kept = 0
    for r in range(GRID[1]):
        for c in range(GRID[0]):
            ys, xs = slice(r * h // GRID[1], (r + 1) * h // GRID[1]), slice(c * w // GRID[0], (c + 1) * w // GRID[0])
            n = int(stable[ys, xs].sum())
            if n >= MIN_CELL_EDGES:
                areas += 1
                kept += hits[ys, xs].sum() / n >= CELL_OK
    if areas < 2:
        return None
    # The shift search: the middle of the reference slid over the current edges (one cross-correlation).
    my, mx = int(h * SEARCH), int(w * SEARCH)
    tmpl = stable[my:h - my, mx:w - mx].astype(np.float32)
    nt = float(tmpl.sum())
    centre = shifted = float(hits[my:h - my, mx:w - mx].sum() / nt) if nt else 0.0
    sx = sy = 0
    if nt:
        res = cv2.matchTemplate(near.astype(np.float32), tmpl, cv2.TM_CCORR) / nt
        iy, ix = np.unravel_index(int(np.argmax(res)), res.shape)
        # A best fit at the very edge of the search is not trusted: the true shift is further, or there is none.
        edge = iy in (0, res.shape[0] - 1) or ix in (0, res.shape[1] - 1)
        if float(res[iy, ix]) - centre > 0.02 and not edge:
            sx, sy, shifted = int(ix) - mx, int(iy) - my, float(res[iy, ix])
    return Match(float(hits.sum() / total), kept / areas, areas, centre, shifted,
                 round(100.0 * sx / w, 1), round(100.0 * sy / h, 1))


def is_moved(match: Match, th: "Thresholds") -> bool:
    """Moved when most areas of the learned view are gone (a turn, a knock, another scene), or when the view is found
    again clearly better somewhere else (a pan or tilt). People and cars hide some areas but leave the others in
    place, and never make the view fit better elsewhere."""
    if match.kept < th.moved_match:
        return True
    far = abs(match.shift_x) >= 100 * MIN_SHIFT or abs(match.shift_y) >= 100 * MIN_SHIFT
    return far and match.shifted - match.centre >= SHIFT_GAIN and match.shifted >= SHIFT_MATCH


@dataclass(frozen=True)
class Measure:
    """Everything measured on one frame."""

    mode: str
    spread: float
    p95: float
    sharpness: float
    edges: np.ndarray
    size: tuple[int, int]   # the frame's own width and height


def measure(frame: np.ndarray, th: Thresholds) -> Measure:
    grey = to_grey(frame)
    spread, p95 = brightness_stats(grey)
    return Measure(mode_of(frame, th), spread, p95, sharpness(grey), edge_map(grey), (frame.shape[1], frame.shape[0]))


def is_covered(m: Measure, th: Thresholds) -> bool:
    return m.spread <= th.flat_spread or m.p95 <= th.dark_level
