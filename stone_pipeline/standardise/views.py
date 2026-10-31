"""A close-up of a slab, made only from the real stone, never invented (the user: close-ups yes, corners no; the side
view is studio.place)."""
import cv2

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import layout


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
