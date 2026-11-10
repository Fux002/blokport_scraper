"""The 3D slab of one photo, from the clean slab a run saved (<sha>.slab.png: the slab squared to its real proportions,
alpha = its own outline): the WebP cut-out and its meta (the outline polygon and the sawn edge colour), as the
"Slab 3D on Blokport" contract has them. Pure and deterministic: the same slab gives the same bytes."""
import json

import cv2
import numpy as np

from stone_pipeline.config.standardise_settings import STANDARDISE as C

# What a 3D slab's pixels are made from, additive meta keys (Mas, 2026-11-10; the contract's parser allows extra keys).
# Exact, for a slab its run recorded (layout.source_of): the stone's long side in real pixels before the layout enlarged
# it, and whether it did. Only a bound, for one published before that was recorded: the long side of the photo its run
# started from. A value that cannot be determined is absent, never false or 0.
SOURCE_LONG_PX, UPSCALED, SOURCE_LONG_PX_MAX = "source_long_px", "upscaled", "source_long_px_max"


def make(slab_png: bytes, **extra) -> tuple[bytes, bytes]:
    """(the WebP cut-out, its meta JSON); `extra` adds keys to the meta (what it is made from: SOURCE_LONG_PX ...).
    Raises ValueError on a slab that cannot be one: no alpha, no stone, unreadable."""
    bgra = cv2.imdecode(np.frombuffer(slab_png, np.uint8), cv2.IMREAD_UNCHANGED)
    if bgra is None or bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("the slab is not an RGBA image")
    return encode(bgra, **extra)


def encode(bgra: np.ndarray, **extra) -> tuple[bytes, bytes]:
    """(the WebP cut-out, its meta JSON) of a BGRA image whose alpha is the stone's outline, at most CUTOUT_LONG_SIDE px;
    `extra` adds keys to the meta (a tile's px_per_mm). Raises ValueError on one that cannot be a cut-out."""
    try:
        return _encode(bgra.copy(), extra)
    except cv2.error as exc:
        raise ValueError(f"opencv: {exc}") from exc


def _encode(bgra, extra):
    stone = bgra[:, :, 3] > C.CUTOUT_ALPHA_CUT
    if not stone.any():
        raise ValueError("no stone in the slab's alpha")
    # Off the stone the colour is the stone's own: the resize and the lossy encode blend across the outline, and whatever
    # lay under the alpha (black, the room) would rim the slab.
    bgra[~stone, :3] = bgra[:, :, :3][stone].mean(0).astype(np.uint8)
    s = C.CUTOUT_LONG_SIDE / max(bgra.shape[:2])
    if s < 1:
        bgra = cv2.resize(bgra, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    stone = (bgra[:, :, 3] > C.CUTOUT_ALPHA_CUT).astype(np.uint8)
    h, w = stone.shape
    if min(h, w) < 2:
        raise ValueError("the slab is too small to have an outline")
    # The outer pixels' centres sit on the frame's edges, so a whole rectangle spans 0..1 like the texture it carries.
    meta = {"version": C.CUTOUT_VERSION, "width_px": w, "height_px": h,
            "outline": [[round(float(x) / (w - 1), 4), round(float(y) / (h - 1), 4)] for x, y in outline(stone)],
            "edge_color": edge_color(bgra, stone), **extra}
    ok, buf = cv2.imencode(".webp", bgra, [cv2.IMWRITE_WEBP_QUALITY, C.CUTOUT_WEBP_QUALITY])
    if not ok:
        raise ValueError("could not encode the cut-out")
    return buf.tobytes(), json.dumps(meta).encode()


def outline(stone: np.ndarray) -> np.ndarray:
    """The stone's outer edge as at most CUTOUT_MAX_POINTS (x, y) pixel points in order. Raises ValueError on no shape."""
    contours, _ = cv2.findContours(stone, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("no stone in the slab's alpha")
    edge = max(contours, key=cv2.contourArea)
    length, eps = cv2.arcLength(edge, True), C.CUTOUT_EPSILON
    while len(points := cv2.approxPolyDP(edge, eps * length, True)) > C.CUTOUT_MAX_POINTS:
        eps *= C.CUTOUT_EPSILON_STEP
    if len(points) < 3:
        raise ValueError("the slab's outline is not a shape")
    return points[:, 0, :]


def edge_color(bgra: np.ndarray, stone: np.ndarray) -> str:
    """The sawn edge's colour: the stone's mean colour, paler and duller."""
    mean = bgra[:, :, :3][stone > 0].reshape(-1, 3).mean(0)[::-1]          # BGR -> RGB
    edge = np.clip(mean * (1 - C.CUTOUT_EDGE_WHITE) + 255 * C.CUTOUT_EDGE_WHITE, 0, 255)
    return "#%02x%02x%02x" % tuple(int(v) for v in edge)
