"""Draws images/icon.png (256 x 256) and images/screenshot-1.png (800 x 450). Everything is drawn: the "camera
views" are the synthetic street of tests/scenes.py, not a photo of a real place.

    python tools/make_images.py          (needs numpy and opencv-python-headless, as the add-on does)

Limits checked by vigiora-addon validate: the icon is a square PNG of 128 to 512 pixels and at most 200 KB;
a screenshot is at most 500 KB and 2000 pixels on its longest side.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]
import scenes as S  # noqa: E402
from tamper_watch.detect import Thresholds, edge_match, measure  # noqa: E402

OUT = ROOT / "images"
# BGR
NAVY, TEAL, WHITE, AMBER, RED, GREEN, INK, PAPER = (84, 37, 23), (166, 184, 20), (255, 255, 255), (11, 158, 245), \
    (60, 60, 220), (94, 160, 34), (40, 30, 25), (232, 241, 245)


def icon() -> np.ndarray:
    ss = 4  # supersampling
    n = 256 * ss
    img = np.full((n, n, 3), 255, np.uint8)
    r = 48 * ss
    cv2.rectangle(img, (r, 0), (n - r, n), NAVY, -1)
    cv2.rectangle(img, (0, r), (n, n - r), NAVY, -1)
    for cx in (r, n - r):
        for cy in (r, n - r):
            cv2.circle(img, (cx, cy), r, NAVY, -1, cv2.LINE_AA)
    c = n // 2
    cv2.circle(img, (c - 14 * ss, c), 74 * ss, TEAL, 22 * ss, cv2.LINE_AA)      # the lens
    cv2.circle(img, (c - 14 * ss, c), 24 * ss, WHITE, -1, cv2.LINE_AA)
    tri = np.array([[c + 70 * ss, c + 4 * ss], [c + 118 * ss, c + 92 * ss], [c + 22 * ss, c + 92 * ss]], np.int32)
    cv2.fillPoly(img, [tri], AMBER, cv2.LINE_AA)                                 # a warning sign over its edge
    cv2.polylines(img, [tri], True, NAVY, 6 * ss, cv2.LINE_AA)
    cv2.line(img, (c + 70 * ss, c + 34 * ss), (c + 70 * ss, c + 64 * ss), NAVY, 10 * ss, cv2.LINE_AA)
    cv2.circle(img, (c + 70 * ss, c + 79 * ss), 6 * ss, NAVY, -1, cv2.LINE_AA)
    return cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)


def tile(view: np.ndarray, label: str, colour, note: str, w: int, h: int) -> np.ndarray:
    t = np.full((h, w, 3), PAPER, np.uint8)
    vh = h - 64
    t[:vh] = cv2.resize(view, (w, vh), interpolation=cv2.INTER_AREA)
    cv2.rectangle(t, (0, vh), (w, h), WHITE, -1)
    cv2.circle(t, (18, vh + 22), 7, colour, -1, cv2.LINE_AA)
    cv2.putText(t, label, (34, vh + 29), cv2.FONT_HERSHEY_SIMPLEX, 0.62, INK, 2, cv2.LINE_AA)
    cv2.putText(t, note, (34, vh + 52), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (90, 90, 90), 1, cv2.LINE_AA)
    return t


def screenshot() -> np.ndarray:
    w = S.world(2, texture=False)
    normal = S.view(w)
    covered = S.with_timestamp(S.flat((18, 18, 20), 0))
    blurred = S.blur(normal, 7)
    moved = S.view(w, dx=22, dy=12, angle=-9)
    # The numbers under the tiles are measured by the add-on's own tests on these very pictures.
    th = Thresholds()
    learn = [measure(S.add_noise(normal, 2, seed=i), th) for i in range(10)]
    ref = np.mean([m.edges for m in learn], axis=0)
    sharp = float(np.median([m.sharpness for m in learn]))
    c, b, m = measure(covered, th), measure(blurred, th), measure(moved, th)
    W, H, gap = 800, 450, 12
    tw, tht = (W - 3 * gap) // 2, (H - 3 * gap) // 2
    img = np.full((H, W, 3), (210, 214, 218), np.uint8)
    tiles = [tile(normal, "tamper:ok", GREEN, "the learned normal view", tw, tht),
             tile(covered, "tamper:covered", RED, f"brightness spread {c.spread:.0f} of 255", tw, tht),
             tile(blurred, "tamper:blurred", AMBER, f"sharpness {b.sharpness / sharp:.2f} of normal", tw, tht),
             tile(moved, "tamper:moved", AMBER, f"{edge_match(ref, m.edges).kept:.2f} of the view in place", tw, tht)]
    for i, t in enumerate(tiles):
        x, y = gap + (i % 2) * (tw + gap), gap + (i // 2) * (tht + gap)
        img[y:y + tht, x:x + tw] = t
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    cv2.imwrite(str(OUT / "icon.png"), icon(), [cv2.IMWRITE_PNG_COMPRESSION, 9])
    cv2.imwrite(str(OUT / "screenshot-1.png"), screenshot(), [cv2.IMWRITE_PNG_COMPRESSION, 9])
    for f in ("icon.png", "screenshot-1.png"):
        print("wrote", OUT / f, (OUT / f).stat().st_size // 1024, "KB")
