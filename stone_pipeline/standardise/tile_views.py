"""A tile listing's views and 3D tile, all drawn from real stone faces (tile_stone), on the standard canvas:

  close    the laid tiles from straight above at true scale, a few tiles and their joints, real pixels enlarged at most
           TILE_CLOSE_MAX_UPSCALE (the main view: the catalog card)
  single   one tile, face-on, on white with the slab views' soft navy shadow (white)
  side     one tile lying flat in a 3/4 view, its true thickness on two sawn edges (studio)
  model    the 3D tile: the single face as a cut-out, its horizontal axis the listing's stored length

Rendered: the laying, joints, eased edges, shadows and the sawn edges. Every stone pixel is a face's own, at its real
scale. Pure and deterministic: the same faces give the same bytes per build (the same OpenCV and NumPy).
"""
from dataclasses import dataclass

import cv2
import numpy as np

from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.standardise import cutout, layout
from stone_pipeline.standardise import tile_pattern as P



@dataclass(frozen=True)
class Tile:
    faces: tuple           # float32 RGB faces, each its length along x (the tile's long side)
    hero: int              # the face shown on its own
    ppm: float             # px per metre of the faces
    long_m: float          # the tile's long and short side and thickness (metres)
    short_m: float
    thick_m: float


@dataclass(frozen=True)
class _Atlas:
    """Every face side by side on one strip, each with a replicated rim so a far (minified) sample never reaches its
    neighbour, and a texture pyramid of it."""
    levels: tuple
    x0: np.ndarray         # each face's left edge on the strip (px)
    y0: int


def _atlas(tile):
    rim = 2 ** (C.TILE_LEVELS - 1)
    padded = [cv2.copyMakeBorder(f, rim, rim, rim, rim, cv2.BORDER_REPLICATE) for f in tile.faces]
    strip = np.concatenate(padded, axis=1)
    widths = np.array([p.shape[1] for p in padded])
    levels = [strip]
    for _ in range(C.TILE_LEVELS - 1):
        levels.append(cv2.pyrDown(levels[-1]))
    return _Atlas(tuple(levels), np.concatenate([[0], np.cumsum(widths)[:-1]]) + rim, rim)


def _sample(levels, sx, sy, footprint):
    """Colours at strip px (sx, sy), from the pyramid level matching each point's footprint (strip px per output px),
    blended between the two nearest levels so distance never shows as a band."""
    lvl = np.clip(np.log2(np.maximum(footprint, 1.0)), 0, C.TILE_LEVELS - 1 - 1e-6)
    lo = np.floor(lvl).astype(np.int64)
    frac = (lvl - lo).astype(np.float32)
    out = np.zeros((sx.size, 3), np.float32)
    cols = 4096                                        # remap maps are 2D and at most 32767 px per side
    for k in range(C.TILE_LEVELS):
        weight = np.where(lo == k, 1 - frac, 0) + np.where(lo + 1 == k, frac, 0)
        use = weight > 0
        if not use.any():
            continue
        m = int(use.sum())
        rows = (m + cols - 1) // cols
        mx, my = np.zeros(rows * cols, np.float32), np.zeros(rows * cols, np.float32)
        mx[:m], my[:m] = sx[use] / 2 ** k, sy[use] / 2 ** k
        got = cv2.remap(levels[k], mx.reshape(rows, cols), my.reshape(rows, cols),
                        cv2.INTER_CUBIC if k == 0 else cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        out[use] += got.reshape(-1, 3)[:m] * weight[use][:, None]
    return out


def _joint_colour(tile):
    """Grout that suits the stone: light for a light stone, dark for a dark one, tinted by the stone's mean."""
    mean = np.mean([f.reshape(-1, 3).mean(0) for f in tile.faces], axis=0)
    l_star = cv2.cvtColor(np.float32([[mean / 255]]), cv2.COLOR_RGB2LAB)[0, 0, 0]
    grey = C.TILE_JOINT_GREY[1] if l_star < C.TILE_JOINT_DARK_STONE_L else C.TILE_JOINT_GREY[0]
    return C.TILE_JOINT_STONE_SHARE * mean + (1 - C.TILE_JOINT_STONE_SHARE) * grey


def _surface(tile, atlas, x, y, footprint, row_span):
    """RGB (N x 3) of the laid floor at floor points x, y (metres, the pattern's own frame): each tile its own face,
    joints and eased edges drawn. footprint: face px per drawn px at each point; row_span: tile_pattern.rows of the
    whole drawing."""
    L, W = tile.long_m, tile.short_m
    pat = P.for_tile(L, W)
    i, j, u, v = P.locate(pat, x, y)
    index, turns = P.assign(pat, i, j, len(tile.faces), row_span)
    ut, vt = u - C.TILE_JOINT_M / 2, v - C.TILE_JOINT_M / 2
    # the point on its face, the tile turned about its centre (a square a quarter at a time; never mirrored)
    su = np.select([turns == 0, turns == 1, turns == 2, turns == 3], [ut, vt, L - ut, W - vt])
    sv = np.select([turns == 0, turns == 1, turns == 2, turns == 3], [vt, L - ut, W - vt, ut])
    sx = atlas.x0[index] + np.clip(su, 0, L) * tile.ppm
    sy = atlas.y0 + np.clip(sv, 0, W) * tile.ppm
    rgb = _sample(atlas.levels, sx.astype(np.float32), sy.astype(np.float32), footprint)
    edge = np.minimum(np.minimum(ut, L - ut), np.minimum(vt, W - vt))      # metres inside the tile; < 0 in a joint
    rgb *= (1 - C.TILE_BEVEL_DARK * np.exp(-np.maximum(edge, 0) / C.TILE_BEVEL_M))[:, None].astype(np.float32)
    # a joint is far thinner than a far pixel: each pixel takes the share of it the tile covers, so no joint breaks up
    cover = np.clip(edge / (footprint / tile.ppm) + 0.5, 0, 1)[:, None].astype(np.float32)
    return rgb * cover + np.float32(_joint_colour(tile)) * (1 - cover)


def _single_scale(face, cap):
    free = 1 - 2 * C.TILE_WHITE_MARGIN
    return min(C.OUT_W * free / face.shape[1], C.OUT_H * free / face.shape[0], cap)


def oriented(tile: Tile, length_m: float, width_m: float) -> np.ndarray:
    """The hero face turned so its horizontal axis is the listing's stored length (a 60 x 80 tile stored with length
    0.6 stands upright)."""
    face = tile.faces[tile.hero]
    return np.ascontiguousarray(np.rot90(face)) if length_m < width_m else face


def single(face: np.ndarray) -> np.ndarray:
    """One tile face-on on white, the slab views' soft shadow; its real pixels enlarged at most TILE_MAX_UPSCALE (a small
    tile sits smaller on the white, sharp, rather than filling it soft)."""
    k = _single_scale(face, C.TILE_MAX_UPSCALE)
    w, h = int(round(face.shape[1] * k)), int(round(face.shape[0] * k))
    rgb = np.full((C.OUT_H, C.OUT_W, 3), 255, np.float32)
    cover = np.zeros((C.OUT_H, C.OUT_W), np.float32)
    x, y = (C.OUT_W - w) // 2, (C.OUT_H - h) // 2
    rgb[y:y + h, x:x + w] = cv2.resize(face, (w, h), interpolation=cv2.INTER_LANCZOS4 if k > 1 else cv2.INTER_AREA)
    cover[y:y + h, x:x + w] = 1
    return layout.shadowed_on_white(rgb, cv2.GaussianBlur(cover, (0, 0), C.EDGE_SOFT))


def _camera(size):
    """Project world points (x along the face's horizontal axis, y away from the viewer, z up) for the 3/4 view."""
    length_m, width_m, thick = size
    centre = np.array([length_m / 2, width_m / 2, thick / 2])
    az, el = np.radians(C.TILE_SIDE_ANGLES[0]), np.radians(C.TILE_SIDE_ANGLES[1])
    eye = centre + C.TILE_SIDE_DISTANCE * max(length_m, width_m) * np.array(
        [np.sin(az) * np.cos(el), -np.cos(az) * np.cos(el), np.sin(el)])
    fwd = (centre - eye) / np.linalg.norm(centre - eye)
    right = np.cross(fwd, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)

    def project(points):
        d = np.asarray(points, np.float64) - eye
        z = d @ fwd
        return np.stack([(d @ right) / z, -(d @ up) / z], axis=1)
    return project


def side_projection(face: np.ndarray, size, ss: int):
    """The studio view's camera: world points (metres: x along `face`'s horizontal axis, y away, z up) to px of the view
    drawn `ss` times larger. The tile fills the frame within TILE_SIDE_MARGIN, its real pixels enlarged at most
    TILE_MAX_UPSCALE."""
    length_m, width_m, thick = size
    project = _camera(size)
    pts = project([(x, y, z) for x in (0, length_m) for y in (0, width_m) for z in (0, thick)])
    span = pts.max(0) - pts.min(0)
    front = project([(0, 0, thick), (length_m, 0, thick)])
    W, H = C.OUT_W * ss, C.OUT_H * ss
    k = min(W * (1 - 2 * C.TILE_SIDE_MARGIN) / span[0], H * (1 - 2 * C.TILE_SIDE_MARGIN) / span[1],
            C.TILE_MAX_UPSCALE * face.shape[1] * ss / float(np.linalg.norm(front[1] - front[0])))
    shift = np.array([W, H]) / 2 - k * (pts.max(0) + pts.min(0)) / 2
    return lambda points: (project(points) * k + shift).astype(np.float32)


def side(face: np.ndarray, size) -> np.ndarray:
    """One tile lying flat, seen from the front right and above: its face in perspective, its true thickness on the two
    sawn edges facing the camera (the face's own last row and column carried across them, in less light, as the slab's
    studio view does), a soft navy shadow under it. size = (horizontal, depth, thickness) of `face` in metres; its real
    pixels are enlarged at most TILE_MAX_UPSCALE."""
    length_m, width_m, thick = size
    ss = C.TILE_SUPERSAMPLE
    W, H = C.OUT_W * ss, C.OUT_H * ss
    to_px = side_projection(face, size, ss)
    out = np.full((H, W, 3), 255, np.float32)
    bottom = np.zeros((H, W), np.float32)
    cv2.fillPoly(bottom, [np.int32(to_px([(0, 0, 0), (length_m, 0, 0), (length_m, width_m, 0), (0, width_m, 0)]))], 1.0)
    tint = np.array(C.WHITE_TINT, np.float32)
    for blur, drop, strength in C.TILE_SIDE_SHADOW:
        s = np.roll(cv2.GaussianBlur(bottom, (0, 0), blur * ss), drop * ss, axis=0)[..., None] * strength
        out = out * (1 - s) + tint * s
    fh, fw = face.shape[:2]
    # The edge strip is stretched across the thickness; bilinear sampling leaks half a strip pixel past its quad, so a
    # 2 px strip would stand a quarter of the thickness proud of the face. At TILE_SIDE_EDGE_PX the leak is invisible.
    rows = C.TILE_SIDE_EDGE_PX

    def paint(out, img, src_quad, world_quad, light=1.0):
        M = cv2.getPerspectiveTransform(np.float32(src_quad), to_px(world_quad))
        col = cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_LINEAR)
        a = cv2.warpPerspective(np.ones(img.shape[:2], np.float32), M, (W, H), flags=cv2.INTER_LINEAR)[..., None]
        return out * (1 - a) + col * light * a

    near = np.ascontiguousarray(np.repeat(face[-1:], rows, axis=0))               # thickness x length
    out = paint(out, near, [(0, 0), (fw, 0), (fw, rows), (0, rows)],
                [(0, 0, thick), (length_m, 0, thick), (length_m, 0, 0), (0, 0, 0)], C.TILE_SIDE_EDGE_LIGHT[1])
    right = np.ascontiguousarray(np.repeat(face[:, -1:], rows, axis=1))           # width x thickness
    out = paint(out, right, [(0, 0), (rows, 0), (rows, fh), (0, fh)],
                [(length_m, width_m, thick), (length_m, width_m, 0), (length_m, 0, 0), (length_m, 0, thick)],
                C.TILE_SIDE_EDGE_LIGHT[0])
    out = paint(out, face, [(0, 0), (fw, 0), (fw, fh), (0, fh)],
                [(0, width_m, thick), (length_m, width_m, thick), (length_m, 0, thick), (0, 0, thick)])
    return np.clip(cv2.resize(out, (C.OUT_W, C.OUT_H), interpolation=cv2.INTER_AREA), 0, 255).astype(np.uint8)


def close_scale(tile: Tile) -> float:
    """The close view's px per metre of floor: as wide as TILE_CLOSE_MIN_TILES tile lengths or more, the real pixels
    enlarged at most TILE_CLOSE_MAX_UPSCALE."""
    return C.OUT_W / max(C.OUT_W / (tile.ppm * C.TILE_CLOSE_MAX_UPSCALE), C.TILE_CLOSE_MIN_TILES * tile.long_m)


def close(tile: Tile) -> np.ndarray:
    """The laid tiles from straight above at true scale (close_scale): each tile its own face, L x W with 3 mm joints."""
    px_per_m = close_scale(tile)
    ys, xs = np.mgrid[0:C.OUT_H, 0:C.OUT_W]
    x = (xs.ravel() + 0.5) / px_per_m - C.TILE_PATTERN_ORIGIN_M[0]
    y = (C.OUT_H - ys.ravel() - 0.5) / px_per_m - C.TILE_PATTERN_ORIGIN_M[1]   # floor y runs up the image, seen from above
    span = P.rows(P.for_tile(tile.long_m, tile.short_m), y.min(), y.max())
    rgb = _surface(tile, _atlas(tile), x, y, np.full(x.shape, tile.ppm / px_per_m), span)
    return np.clip(rgb.reshape(C.OUT_H, C.OUT_W, 3), 0, 255).astype(np.uint8)


def model(face: np.ndarray, ppm: float) -> tuple[bytes, bytes]:
    """The 3D tile: (WebP cut-out, meta JSON) of the face as given (its horizontal axis the listing's stored length),
    resampled (Lanczos) to CUTOUT_LONG_SIDE px on its long side, wholly opaque; the meta records px_per_mm, the stone's
    real source pixels per millimetre before that resample (not the cut-out's own scale)."""
    h, w = face.shape[:2]
    k = C.CUTOUT_LONG_SIDE / max(h, w)
    size = (int(round(w * k)), int(round(h * k)))
    rgb = np.clip(cv2.resize(face, size, interpolation=cv2.INTER_LANCZOS4), 0, 255).astype(np.uint8)
    bgra = np.dstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), np.full(rgb.shape[:2], 255, np.uint8)])
    return cutout.encode(bgra, px_per_mm=round(ppm / 1000, 4))


def encode_jpeg(rgb: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, C.TILE_JPEG_QUALITY])
    if not ok:
        raise ValueError("could not encode a tile view")
    return buf.tobytes()
