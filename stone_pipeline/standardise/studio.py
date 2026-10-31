"""The real slab in Blokport's studio scene: the base image its storefront already uses (a slab at three-quarter view on
two chrome feet, white backdrop, soft shadow), with that slab replaced by the real one. No model: the real stone, only
put in the scene's perspective, at its own proportions, standing on the same foot line; the feet's clips stay in front
of it. The scene's slab face was measured once from the base image (assets/studio_base.jpg)."""
import functools
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from stone_pipeline.config.settings import STANDARDISE as C

BASE_PATH = Path(__file__).parent / "assets" / "studio_base.jpg"
FACE = np.array([[211, 258.1], [804, 185.1], [795, 833.4], [213, 702.6]], np.float32)   # TL, TR, BR, BL of its slab face
EDGE_OUT = np.array([[813, 184.0], [804, 836.0]], np.float32)                           # its thickness face's outer top, bottom
BASE_ASPECT = 1.637                       # that slab's true width / height (from the face's perspective, focal 1687 px)
FOCAL, CENTRE = 1687.0, 512.0
BOTTOM = (0.2246, 654.8)                  # its bottom edge: y = a x + b
# The feet: below the slab's bottom edge the whole foot; above it only each clip's own outline (traced at 8x).
FEET = ((248, 704, 352, 762), (548, 770, 668, 832))
CLIP_TOPS = (((285, 718.8), (290, 717), (302, 715), (306, 712.5), (311, 714), (316, 713), (318, 716), (318.5, 722), (320, 726.7)),
             ((583, 785.7), (590, 784), (600, 781.5), (604, 778), (610, 777), (616, 778.5), (618, 782), (618.5, 793.7)))


@functools.lru_cache(maxsize=1)
def _scene():
    """(the base scene without its slab, the clips' alpha): behind the old slab the white backdrop is filled in smoothly."""
    base = np.asarray(Image.open(BASE_PATH).convert("RGB")).astype(np.float32)
    h, w = base.shape[:2]
    old = np.zeros((h, w), np.uint8)
    cv2.fillPoly(old, [FACE.astype(np.int32), np.array([FACE[1], EDGE_OUT[0], EDGE_OUT[1], FACE[2]], np.int32)], 1)
    old = cv2.dilate(old, np.ones((5, 5), np.uint8))
    clips = np.zeros((h, w), np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    below = yy > BOTTOM[0] * xx + BOTTOM[1] + 0.5
    for (x0, y0, x1, y1), top in zip(FEET, CLIP_TOPS):
        clips[y0:y1, x0:x1] |= below[y0:y1, x0:x1].astype(np.uint8)
        cv2.fillPoly(clips, [np.array(top, np.int32)], 1)
    hole = (old > 0) & (clips == 0)
    small = cv2.resize(np.clip(base, 0, 255).astype(np.uint8), (w // 4, h // 4), interpolation=cv2.INTER_AREA)
    gap = cv2.dilate(cv2.resize(hole.astype(np.uint8), (w // 4, h // 4), interpolation=cv2.INTER_NEAREST), np.ones((3, 3), np.uint8))
    filled = cv2.inpaint(small, gap, 6, cv2.INPAINT_TELEA).astype(np.float32)
    filled = cv2.GaussianBlur(cv2.resize(filled, (w, h), interpolation=cv2.INTER_CUBIC), (0, 0), 6)
    plate = base.copy()
    plate[hole] = filled[hole]
    return plate, base, clips.astype(np.float32)


def face(aspect):
    """The face (TL, TR, BR, BL, base px) of a slab of this true width/height in the scene: the base face's perspective,
    the same foot line, as tall as its proportions make it."""
    H = cv2.getPerspectiveTransform(np.float32([[0, 1], [1, 1], [1, 0], [0, 0]]), FACE)
    r = BASE_ASPECT / aspect
    return cv2.perspectiveTransform(np.float32([[[0, r], [1, r], [1, 0], [0, 0]]]), H)[0]


def true_aspect(q):
    """The true width/height of the rectangle imaged as q (TL, TR, BR, BL), seen by the scene's camera."""
    m1, m2, m3, m4 = (np.array([*q[i], 1.0]) for i in (3, 2, 0, 1))
    k2 = np.cross(m1, m4) @ m3 / (np.cross(m2, m4) @ m3)
    k3 = np.cross(m1, m4) @ m2 / (np.cross(m3, m4) @ m2)
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    Ai = np.linalg.inv(np.array([[FOCAL, 0, CENTRE], [0, FOCAL, CENTRE], [0, 0, 1]]))
    return float(np.sqrt((n2 @ Ai.T @ Ai @ n2) / (n3 @ Ai.T @ Ai @ n3)))


def _frame(q):
    """(x0, y0, width, height) in base px: one camera for every slab, raised only where a tall slab needs the room."""
    top = min(C.STUDIO_FRAME[1], float(q[:, 1].min()) - C.STUDIO_HEADROOM)
    fh = C.STUDIO_FRAME[2] - top
    fw = fh * C.OUT_W / C.OUT_H
    return C.STUDIO_FRAME[0] - fw / 2, top, fw, fh


def to_frame(pt, q):
    """A base-px point's place on the finished image."""
    x0, y0, fw, fh = _frame(q)
    return (pt[0] - x0) * C.OUT_W / fw, (pt[1] - y0) * C.OUT_H / fh


def place(slabs):
    """The finished studio image of one slab, or an open book joined into one face."""
    rgb = np.concatenate([s for s, _ in slabs], axis=1).astype(np.float32)
    alpha = np.concatenate([a for _, a in slabs], axis=1).astype(np.float32)
    th, tw = rgb.shape[:2]
    q = face(tw / th)
    x0, y0, fw, fh = _frame(q)
    # Everything is drawn straight onto the output frame: the scene and the slab each mapped once, at full quality.
    S = np.array([[C.OUT_W / fw, 0, -x0 * C.OUT_W / fw], [0, C.OUT_H / fh, -y0 * C.OUT_H / fh], [0, 0, 1]], np.float32)
    size = (C.OUT_W, C.OUT_H)
    plate, base, clips = _scene()
    warp = lambda img, M, flags=cv2.INTER_LANCZOS4: cv2.warpPerspective(img, M, size, flags=flags, borderMode=cv2.BORDER_REPLICATE)
    out = warp(plate, S)
    keep = cv2.warpPerspective(clips, S, size, flags=cv2.INTER_LINEAR)[..., None]
    corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]])
    dst = cv2.perspectiveTransform(q[None], S)[0]
    M = cv2.getPerspectiveTransform(corners, dst)
    stone = cv2.warpPerspective(rgb, M, size, flags=cv2.INTER_LANCZOS4)
    a = cv2.warpPerspective(alpha, M, size, flags=cv2.INTER_LINEAR)[..., None]
    # The sawn edge on the right is the same stone: its last columns stretched across the scene's thickness, in less light.
    off_top, off_bottom = (EDGE_OUT - FACE[1:3]) * np.float32([C.OUT_W / fw, C.OUT_H / fh])
    tr, br = dst[1], dst[2]
    strip = np.ascontiguousarray(rgb[:, -max(2, tw // 200):])
    E = cv2.getPerspectiveTransform(np.float32([[0, 0], [strip.shape[1], 0], [strip.shape[1], th], [0, th]]),
                                    np.float32([tr, tr + off_top, br + off_bottom, br]))
    edge = cv2.warpPerspective(strip, E, size, flags=cv2.INTER_LINEAR) * C.STUDIO_EDGE_LIGHT
    side = cv2.warpPerspective(np.ones(strip.shape[:2], np.float32), E, size, flags=cv2.INTER_LINEAR)[..., None]
    out = out * (1 - side) + edge * side
    out = out * (1 - a) + stone * a
    out = out * (1 - keep) + warp(base, S) * keep                     # the clips stand in front of the slab's foot
    return np.clip(out, 0, 255).astype(np.uint8)
