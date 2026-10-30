"""Extra views of a slab, made only from the real stone, never invented (the user: angled views and close-ups yes, corners
no; the back of a slab is never shown, we have no photo of it)."""
import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import layout


def _turned(w, h, side):
    """The canvas's homography for the slab turned about the vertical through its centre: the far side smaller, about
    the floor line, so the stand still stands on the floor."""
    floor = h * (1 - C.FLOOR_SHARE)
    narrow, near_grow = C.ANGLE_FAR, C.ANGLE_NEAR
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)

    def about_floor(y, scale):
        return floor + (y - floor) * scale
    left, right = (narrow, near_grow) if side == "left" else (near_grow, narrow)
    inset = C.ANGLE_NARROW * w
    x0, x1 = (inset, w) if side == "left" else (0, w - inset)
    dst = np.array([[x0, about_floor(0, left)], [x1, about_floor(0, right)], [x1, about_floor(h, right)], [x0, about_floor(h, left)]], np.float32)
    return cv2.getPerspectiveTransform(src, dst)


def angled(slabs, key, side):
    """(canvas with the slab and stand turned towards `side`, solid mask, stand mask): the room stays, so only the
    slab turns. The cut-out layer is separated from any room by rendering it on black and on white."""
    dark = layout.compose(slabs, key, room=np.zeros((C.OUT_H, C.OUT_W, 3), np.float32)).astype(np.float32)
    light = layout.compose(slabs, key, room=np.full((C.OUT_H, C.OUT_W, 3), 255, np.float32)).astype(np.float32)
    alpha = np.clip(1 - (light - dark).mean(axis=2) / 255, 0, 1)
    H = _turned(C.OUT_W, C.OUT_H, side)
    size = (C.OUT_W, C.OUT_H)
    colour = cv2.warpPerspective(dark, H, size, flags=cv2.INTER_CUBIC).clip(0, 255)
    cover = cv2.warpPerspective(alpha, H, size, flags=cv2.INTER_LINEAR)[..., None]
    room = layout.room_for(key, len(slabs) > 1)
    img = np.clip(room * (1 - cover) + colour, 0, 255).astype(np.uint8)
    solid, stand = layout.masks(slabs, key)
    warp = lambda m: (cv2.warpPerspective(m.astype(np.float32), H, size, flags=cv2.INTER_LINEAR) > 0.5).astype(np.uint8)
    return img, warp(solid), warp(stand)


def close_up(slab, alpha, esr):
    """A close-up of the real stone: the middle of the squared slab at the canvas's shape, enlarged with real detail."""
    h, w = slab.shape[:2]
    cw = int(C.CLOSEUP_SHARE * w)
    ch = int(round(cw * C.OUT_H / C.OUT_W))
    if ch > h:
        ch = h
        cw = int(round(ch * C.OUT_W / C.OUT_H))
    x, y = (w - cw) // 2, (h - ch) // 2
    if alpha[y:y + ch, x:x + cw].min() < 0.5:
        return None
    crop = slab[y:y + ch, x:x + cw]
    factor = C.OUT_W / crop.shape[1]
    big = layout.upscale(crop, esr, factor) if factor > 1 else crop
    return cv2.resize(big, (C.OUT_W, C.OUT_H), interpolation=cv2.INTER_AREA)
