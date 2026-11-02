"""The real stone a tile is cut from: one slab's published 3D cut-out (squared to the slab's listed size, cleaned and lit
by the slab standardiser), at a known scale, and the tile faces a cutter would plan on it.

Nothing is synthesised: every tile face is a crop of these pixels, turned only as a real tile can be laid (never mirrored:
a stone's back is another stone). A cut-out that cannot be trusted as stone is refused (Refused) rather than used.
"""
from dataclasses import dataclass

import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks


class Refused(ValueError):
    """This slab's cut-out is not used for tiles (the reason is the message)."""


@dataclass(frozen=True)
class Stone:
    rgb: np.ndarray        # float32 H x W x 3, 0..255; x along the slab's length
    valid: np.ndarray      # bool H x W: stone a tile may be cut from
    ppm: float             # px per metre


@dataclass(frozen=True)
class Cuts:
    origins: tuple         # ((x0, y0), ...) of each face in stone px, sorted
    across: bool           # True: the tile's length runs along the slab's width (y)
    length_px: int         # the face's size along the tile's length and width
    width_px: int


def decode(webp: bytes) -> np.ndarray:
    """RGBA uint8 of a published cut-out. A WebP without alpha is wholly opaque (the encoder drops an alpha that is
    opaque everywhere: a cut-out that is a whole rectangle). Refused when it is not an image."""
    img = cv2.imdecode(np.frombuffer(webp, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] not in (3, 4):
        raise Refused("the cut-out is not a colour image")
    return cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA if img.shape[2] == 4 else cv2.COLOR_BGR2RGBA)


def grid_score(rgba: np.ndarray) -> float:
    """How strongly the face carries straight lines along both of its axes (a crosshatch): the lesser of the two axes'
    fine-detail power over that of the frequencies beside them, averaged over windows wholly inside the stone. Stone
    scores about 1 (a vein or groove raises one axis only)."""
    w = C.TILE_GRID_WINDOW
    alpha = (rgba[:, :, 3] > C.CUTOUT_ALPHA_CUT).astype(np.uint8)
    inset = 2 * C.TILE_GRID_INSET_PX + 1
    inner = cv2.erode(alpha, np.ones((inset, inset), np.uint8), borderValue=0) > 0
    grey = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2GRAY).astype(np.float32)
    detail = grey - cv2.GaussianBlur(grey, (0, 0), C.TILE_GRID_DETAIL_SIGMA)
    hann = np.outer(np.hanning(w), np.hanning(w)).astype(np.float32)
    power, n = np.zeros((w, w), np.float64), 0
    h, wd = grey.shape
    for y in range(0, h - w + 1, C.TILE_GRID_STEP):
        for x in range(0, wd - w + 1, C.TILE_GRID_STEP):
            if inner[y:y + w, x:x + w].all():
                win = detail[y:y + w, x:x + w]
                power += np.abs(np.fft.fft2((win - win.mean()) * hann)) ** 2
                n += 1
    if n == 0:
        raise Refused("the cut-out has no stone area large enough to check")
    power = np.fft.fftshift(power / n)
    c, k = w // 2, np.arange(w) - w // 2
    band = (np.abs(k) >= C.TILE_GRID_BAND[0]) & (np.abs(k) <= int(C.TILE_GRID_BAND[1] * w))
    near, far = C.TILE_GRID_OFF_AXIS
    beside = [d for d in range(-far, far + 1) if abs(d) >= near]
    along_x = power[c, band].mean() / np.mean([power[c + d, band] for d in beside])
    along_y = power[band, c].mean() / np.mean([power[band, c + d] for d in beside])
    return float(min(along_x, along_y))


def from_cutout(rgba: np.ndarray, length_m: float, width_m: float) -> Stone:
    """The stone of a slab's cut-out, its x along the slab's listed length. Refused: the cut-out carries a crosshatch
    (TILE_GRID_MAX) or is not at the slab's listed proportions (its scale would be a guess)."""
    if (score := grid_score(rgba)) > C.TILE_GRID_MAX:
        raise Refused(f"a crosshatch over the face (grid score {score:.1f} > {C.TILE_GRID_MAX})")
    h, w = rgba.shape[:2]
    long_m, short_m = max(length_m, width_m), min(length_m, width_m)
    if h > w:                                          # the cut-out's long side is the slab's length
        rgba = np.ascontiguousarray(np.rot90(rgba))
        h, w = w, h
    if abs((w / h) / (long_m / short_m) - 1) > C.TILE_ASPECT_TOLERANCE:
        raise Refused(f"the cut-out ({w}x{h} px) is not at the slab's proportions ({long_m:g} x {short_m:g} m)")
    rgb, alpha = rgba[:, :, :3], (rgba[:, :, 3] > C.CUTOUT_ALPHA_CUT).astype(np.uint8)
    ppm = float(np.sqrt((w / long_m) * (h / short_m)))
    inset = max(1, int(round(C.TILE_STONE_INSET_M * ppm)))
    valid = cv2.erode(alpha, np.ones((2 * inset + 1,) * 2, np.uint8), borderValue=0) > 0
    valid &= marks.flare_mask(np.ascontiguousarray(rgb), alpha) == 0
    return Stone(rgb.astype(np.float32), valid, ppm)


def usable_area(stone: Stone) -> float:
    """The stone a tile may be cut from, in square metres."""
    return float(stone.valid.sum()) / stone.ppm ** 2


def _grid(valid, integral, lp, wp):
    """The faces of one orientation: a cutter's grid of lp x wp px from the trimmed corner of the stone's bounding box,
    each kept only when it lies wholly on valid stone."""
    ys, xs = np.nonzero(valid)
    if ys.size == 0:
        return []
    x_start, y_start, x_end, y_end = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    found = []
    for y0 in np.arange(y_start, y_end - wp + 1e-6, wp):
        for x0 in np.arange(x_start, x_end - lp + 1e-6, lp):
            a, b, c, d = int(round(x0)), int(round(y0)), int(round(x0 + lp)), int(round(y0 + wp))
            if integral[d, c] - integral[b, c] - integral[d, a] + integral[b, a] == (c - a) * (d - b):
                found.append((a, b))
    return found


def cuts(stone: Stone, long_m: float, short_m: float) -> Cuts:
    """The distinct faces of a long_m x short_m tile on the stone: the cutter's grid in whichever orientation gives more
    (the tile's length along the slab's length on a tie). Faces never overlap: each is its own piece of stone."""
    lp, wp = int(round(long_m * stone.ppm)), int(round(short_m * stone.ppm))
    integral = cv2.integral(stone.valid.astype(np.uint8))
    along, across = _grid(stone.valid, integral, lp, wp), _grid(stone.valid, integral, wp, lp)
    turned = len(across) > len(along)
    return Cuts(tuple(sorted(across if turned else along)), turned, lp, wp)


def face(stone: Stone, cuts: Cuts, index: int) -> np.ndarray:
    """One tile's face (float32 RGB), its length along x: the cut, turned a quarter when it lies across the slab."""
    x0, y0 = cuts.origins[index]
    if cuts.across:
        crop = stone.rgb[y0:y0 + cuts.length_px, x0:x0 + cuts.width_px]
        return np.ascontiguousarray(np.rot90(crop))
    return np.ascontiguousarray(stone.rgb[y0:y0 + cuts.width_px, x0:x0 + cuts.length_px])


def faces(stone: Stone, cuts: Cuts) -> list[np.ndarray]:
    return [face(stone, cuts, n) for n in range(len(cuts.origins))]


def hero(stone: Stone, cuts: Cuts) -> int:
    """The face nearest the stone's centre (the first on a tie): the single tile shown on its own."""
    h, w = stone.valid.shape
    dx, dy = (cuts.width_px, cuts.length_px) if cuts.across else (cuts.length_px, cuts.width_px)
    return min(range(len(cuts.origins)),
               key=lambda n: ((cuts.origins[n][0] + dx / 2 - w / 2) ** 2 + (cuts.origins[n][1] + dy / 2 - h / 2) ** 2, n))
