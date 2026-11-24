"""Measures the blurred and moved scores against their thresholds, for changes that must NOT raise an alarm and for
real tampering that must. Prints one table per scene.

    python tools/false_alarm_check.py                     # three synthetic scenes (tests/scenes.py)
    python tools/false_alarm_check.py clip.mp4 ...        # also frames of your own videos (a FIXED camera: a
                                                           # hand-held or tracking shot really moves, and is reported)

For a video, up to 40 frames spread over the clip are taken, the first 10 are learned, and the changes are applied to a later frame.
The night-vision switch is measured against the day view on purpose: in the add-on a grey picture is judged against
its own, separately learned night view, so this is the hard case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import scenes as S  # noqa: E402
from tamper_watch.detect import Thresholds, edge_match, is_covered, is_moved, measure  # noqa: E402

TH = Thresholds()


def shift_crop(img: np.ndarray, dx: float = 0, dy: float = 0, angle: float = 0, zoom: float = 0.8) -> np.ndarray:
    """A move of a real camera, imitated on a frame: a crop of `zoom` of it, offset and turned, scaled back."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2 + dx / 100 * w * zoom, h / 2 + dy / 100 * h * zoom), angle, 1 / zoom)
    m[0, 2] -= dx / 100 * w * zoom
    m[1, 2] -= dy / 100 * h * zoom
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def cases(base: np.ndarray, moved: dict[str, np.ndarray]) -> list[tuple[str, bool, np.ndarray]]:
    """(name, should alarm, frame)."""
    out = [
        ("unchanged", False, base),
        ("brightness -50%", False, S.scale_brightness(base, 0.5)),
        ("brightness +50%", False, S.scale_brightness(base, 1.5)),
        ("sensor noise sigma 8", False, S.add_noise(base, 8)),
        ("sensor noise sigma 15", False, S.add_noise(base, 15)),
        ("colour to grey (night switch)", False, S.to_grey_bgr(base)),
        ("grey, darker, noisy", False, S.add_noise(S.scale_brightness(S.to_grey_bgr(base), 0.5), 10)),
        ("object 15% at left", False, S.with_blob(base, 0.15, 0.2)),
        ("object 15% at centre", False, S.with_blob(base, 0.15, 0.5)),
        ("object 15% at right", False, S.with_blob(base, 0.15, 0.8)),
        ("blur sigma 2", True, S.blur(base, 2)),
        ("blur sigma 3 + noise 15", True, S.add_noise(S.blur(base, 3), 15)),
    ]
    out += [(k, True, v) for k, v in moved.items()]
    return out


def table(title: str, learn: list[np.ndarray], tests: list[tuple[str, bool, np.ndarray]]) -> tuple[int, int]:
    ms = [measure(f, TH) for f in learn]
    ref = np.mean([m.edges for m in ms], axis=0)
    normal = float(np.median([m.sharpness for m in ms]))
    print(f"\n{title}   (normal sharpness {normal:.3f}; lasting edges {100 * float((ref >= 0.6).mean()):.1f}% of the picture)")
    print(f"  {'change':34s} {'sharpness':>9s} {'areas':>6s} {'middle in place':>16s} {'shift %':>11s}  verdict   expected")
    print(f"  {'':34s} {'(<' + str(TH.blur_ratio) + ')':>9s} {'(<' + str(TH.moved_match) + ')':>6s} {'-> best shifted':>16s}")
    false_alarms = misses = 0
    for name, should, frame in tests:
        m = measure(frame, TH)
        r = m.sharpness / normal
        mt = edge_match(ref, m.edges)
        if is_covered(m, TH):
            verdict = "covered"
        elif r < TH.blur_ratio:
            verdict = "blurred"
        elif mt is not None and is_moved(mt, TH):
            verdict = "moved"
        else:
            verdict = "ok"
        ok = (verdict != "ok") == should
        false_alarms += not ok and not should
        misses += not ok and should
        kept = f"{mt.kept:.2f}" if mt else "-"
        mid = f"{mt.centre:.2f} -> {mt.shifted:.2f}" if mt else "-"
        sh = f"{mt.shift_x:+.1f},{mt.shift_y:+.1f}" if mt else "-"
        print(f"  {name:34s} {r:9.2f} {kept:>6s} {mid:>16s} {sh:>11s}  {verdict:9s} {'alarm' if should else 'no alarm'}{'' if ok else ('   <-- MISSED' if should else '   <-- FALSE ALARM')}")
    return false_alarms, misses


def video_frames(path: str, n: int = 40) -> list[np.ndarray]:
    """Up to n frames spread over the clip, at least 0.5 s apart."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    every_sec = max(0.5, cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps / n)
    out, i = [], 0
    while len(out) < n:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * every_sec * fps))
        ok, f = cap.read()
        if not ok:
            break
        if f.shape[1] > 640:
            f = cv2.resize(f, (640, round(f.shape[0] * 640 / f.shape[1])), interpolation=cv2.INTER_AREA)
        out.append(f)
        i += 1
    cap.release()
    return out


def main(argv: list[str]) -> int:
    fa = mi = 0

    def add(r: tuple[int, int]) -> None:
        nonlocal fa, mi
        fa, mi = fa + r[0], mi + r[1]

    for seed in (1, 2, 3):
        w = S.world(seed)
        learn = [S.add_noise(S.view(w), 3, seed=10 + i) if i % 3 else
                 S.with_blob(S.add_noise(S.view(w), 3, seed=10 + i), 0.10, i / 10, seed=i) for i in range(10)]
        base = S.add_noise(S.view(w), 3, seed=99)
        moved = {"pan 3%": S.view(w, dx=3), "pan 8%": S.view(w, dx=8), "tilt 5%": S.view(w, dy=5),
                 "rotate 10 degrees": S.view(w, angle=10), "rotate 15 degrees": S.view(w, angle=15)}
        add(table(f"Synthetic scene {seed}", learn, cases(base, moved)))
    for path in argv:
        frames = video_frames(path)
        if len(frames) < 12:
            print(f"\n{path}: too short (need about 6 seconds)")
            continue
        learn, later = frames[:10], frames[10:]
        tests = [(f"later frame {i + 1} (real activity)", False, f) for i, f in enumerate(later)]
        print(f"\n{Path(path).name}: {len(frames)} frames, learned on the first 10")
        # Moves are imitated on the quietest later frame (the one closest to the learned view): passing traffic is
        # measured on every frame in the first table, and a real move is judged over several samples anyway.
        ms = [measure(f, TH) for f in learn]
        ref = np.mean([m.edges for m in ms], axis=0)
        quiet = max(later, key=lambda f: getattr(edge_match(ref, measure(f, TH).edges), "in_place", 0.0))
        moved = {"pan 3% (crop)": shift_crop(quiet, dx=3), "pan 8% (crop)": shift_crop(quiet, dx=8),
                 "tilt 5% (crop)": shift_crop(quiet, dy=5), "rotate 10 degrees (crop)": shift_crop(quiet, angle=10)}
        # the crop moves zoom the picture too, so the learned view for them is the same crop unmoved
        add(table(f"Video {Path(path).name}", learn, tests + cases(later[0], {})))
        crop_learn = [shift_crop(f) for f in learn]
        add(table(f"Video {Path(path).name}, imitated moves (crop of 80%)", crop_learn,
                  [("unmoved crop", False, shift_crop(quiet))] + [(k, True, v) for k, v in moved.items()]))
    print(f"\n{fa} false alarm(s), {mi} missed tampering(s), one frame each (the add-on also needs 3 in a row).")
    return 1 if fa else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
