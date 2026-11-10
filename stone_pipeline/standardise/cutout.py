"""The 3D slab of one photo, from the clean slab a run saved (<sha>.slab.png: the slab squared to its real proportions,
alpha = its own outline): the WebP cut-out and its meta (the outline polygon and the sawn edge colour), as the
"Slab 3D on Blokport" contract has them. Pure and deterministic: the same slab gives the same bytes."""
import json

import cv2
import numpy as np

from stone_pipeline.config.standardise_settings import STANDARDISE as C


def make(slab_png: bytes) -> tuple[bytes, bytes]:
    """(the WebP cut-out, its meta JSON). Raises ValueError on a slab that cannot be one: no alpha, no stone, unreadable."""
    bgra = cv2.imdecode(np.frombuffer(slab_png, np.uint8), cv2.IMREAD_UNCHANGED)
    if bgra is None or bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("the slab is not an RGBA image")
    return encode(bgra)


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


def outline_size(meta: dict) -> tuple[float, float]:
    """(width, height) in px of a cut-out's outline (its JSON meta): the outline's bounding box times the cut-out's px,
    as Blokport's renders measure it. ValueError on a meta that is not one."""
    try:
        us, vs = zip(*((float(u), float(v)) for u, v in meta["outline"]))
        return (max(us) - min(us)) * float(meta["width_px"]), (max(vs) - min(vs)) * float(meta["height_px"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a cut-out outline: {exc!r}") from exc


def aspect_off(w_px: float, h_px: float, length: float, width: float) -> float:
    """How many percent a cut-out of w x h px (its width along the slab's length) is off the listed length:width.
    ValueError on a size that is not positive."""
    if min(w_px, h_px, length, width) <= 0:
        raise ValueError(f"not a size: cut-out {w_px} x {h_px} px, listed {length} x {width}")
    return abs((w_px / h_px) / (length / width) - 1) * 100


def aspect_misfit(w_px: float, h_px: float, length: float, width: float) -> float | None:
    """aspect_off when it is over PHOTO_ASPECT_TOLERANCE_PCT, None within it (inclusive): the one proportion rule for a
    3D slab and a tile's stone."""
    off = aspect_off(w_px, h_px, length, width)
    return off if off > C.PHOTO_ASPECT_TOLERANCE_PCT else None
