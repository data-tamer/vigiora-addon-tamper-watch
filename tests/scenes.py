"""Synthetic camera pictures for the tests and tools/false_alarm_check.py. Drawn with numpy and OpenCV: no photo.

A "world" larger than the view is drawn once (sky, buildings with windows, a road, trees, fine texture); a view is a
crop of it, so a camera that turns sees new things come in from the side, as a real one does.
"""
from __future__ import annotations

import cv2
import numpy as np

VIEW_W, VIEW_H = 640, 360


def world(seed: int = 1, w: int = 1100, h: int = 620, texture: bool = True) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    horizon = int(h * 0.45)
    for y in range(horizon):  # sky
        t = y / horizon
        img[y, :] = (235 - 60 * t, 200 - 40 * t, 150 - 30 * t)
    img[horizon:, :] = (95, 110, 105)  # ground
    x = 0
    while x < w:  # buildings
        bw, bh = int(rng.integers(70, 170)), int(rng.integers(120, 260))
        col = tuple(float(c) for c in rng.integers(60, 200, 3))
        cv2.rectangle(img, (x, horizon - bh), (x + bw, horizon + 20), col, -1)
        for wy in range(horizon - bh + 12, horizon, 28):
            for wx in range(x + 10, x + bw - 18, 24):
                cv2.rectangle(img, (wx, wy), (wx + 11, wy + 15), (40, 45, 60) if rng.random() < 0.7 else (180, 210, 230), -1)
        x += bw + int(rng.integers(5, 40))
    road = np.array([[int(w * 0.25), h], [int(w * 0.45), horizon + 20], [int(w * 0.55), horizon + 20], [int(w * 0.8), h]])
    cv2.fillPoly(img, [road], (70, 70, 72))
    for i in range(8):  # lane marks
        t0, t1 = i / 8, i / 8 + 0.05
        p0 = (int(w * (0.5 + 0.0 * t0)), int(horizon + 20 + (h - horizon - 20) * t0))
        p1 = (int(w * 0.5), int(horizon + 20 + (h - horizon - 20) * t1))
        cv2.line(img, p0, p1, (220, 220, 210), 2 + i // 3)
    for _ in range(9):  # trees
        cx, cy = int(rng.integers(0, w)), int(rng.integers(horizon + 10, h - 40))
        r = int(rng.integers(18, 45))
        cv2.rectangle(img, (cx - 4, cy), (cx + 4, cy + r + 20), (40, 60, 80), -1)
        cv2.circle(img, (cx, cy), r, (50, float(rng.integers(110, 160)), 60), -1)
    tex = cv2.GaussianBlur(rng.normal(0, 9, (h, w)).astype(np.float32), (0, 0), 1.0)
    if texture:
        img += tex[:, :, None]
    return np.clip(img, 0, 255).astype(np.uint8)


def view(w: np.ndarray, dx: float = 0.0, dy: float = 0.0, angle: float = 0.0) -> np.ndarray:
    """A VIEW_W x VIEW_H picture of the world. dx, dy: offset in percent of the view's width and height; angle in
    degrees (a camera knocked or turned)."""
    H, W = w.shape[:2]
    cx, cy = W / 2 + dx / 100 * VIEW_W, H / 2 + dy / 100 * VIEW_H
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    m[0, 2] += VIEW_W / 2 - cx
    m[1, 2] += VIEW_H / 2 - cy
    return cv2.warpAffine(w, m, (VIEW_W, VIEW_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def scale_brightness(img: np.ndarray, factor: float) -> np.ndarray:
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def add_noise(img: np.ndarray, sigma: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(img.astype(np.float32) + rng.normal(0, sigma, img.shape), 0, 255).astype(np.uint8)


def to_grey_bgr(img: np.ndarray) -> np.ndarray:
    """Night vision as a camera sends it: three equal channels."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.merge([g, g, g])


def blur(img: np.ndarray, sigma: float) -> np.ndarray:
    return cv2.GaussianBlur(img, (0, 0), sigma)


def with_blob(img: np.ndarray, share: float = 0.15, x: float = 0.5, seed: int = 0) -> np.ndarray:
    """A person- or van-like object covering `share` of the picture, centred at x (0..1 of the width)."""
    rng = np.random.default_rng(seed)
    out = img.copy()
    h, w = out.shape[:2]
    bh = int(h * 0.8)
    bw = int(share * w * h / bh)
    x0 = int(np.clip(x * w - bw / 2, 0, w - bw))
    y0 = h - bh - 5
    col = tuple(float(c) for c in rng.integers(20, 230, 3))
    cv2.rectangle(out, (x0, y0), (x0 + bw, y0 + bh), col, -1)
    cv2.rectangle(out, (x0 + bw // 5, y0 + bh // 6), (x0 + 4 * bw // 5, y0 + bh // 3), (230, 230, 230), -1)
    return out


def flat(colour: tuple[int, int, int], noise: float = 2.0, seed: int = 0) -> np.ndarray:
    img = np.zeros((VIEW_H, VIEW_W, 3), np.uint8)
    img[:] = colour
    return add_noise(img, noise, seed) if noise else img


def with_timestamp(img: np.ndarray) -> np.ndarray:
    """A white date and time burned into the corner, as most cameras do."""
    out = img.copy()
    cv2.putText(out, "2026-11-24 21:14:07", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(out, "Camera 1", (VIEW_W - 140, VIEW_H - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out
