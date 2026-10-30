"""A texture close-up of a slab, still put on the standard stand (the user's choice): the stone runs off all four
edges of the photo (a whole-slab photo always shows background along a side). Sample cards and labels erased, lit,
cropped to the slab product's proportions; skipped when blurred. Laid out by compose.compose, like every slab.
Straps stay as shot (the user's choice, 2026-10-30): never erased.
"""
from stone_pipeline.config.settings import STANDARDISE as C
import cv2
import numpy as np

from stone_pipeline.standardise import layout as compose
from stone_pipeline.standardise import outline as openbook
from stone_pipeline.standardise import marks as proto

def _sharp(rgb):
    g = cv2.cvtColor(cv2.resize(rgb, (1024, int(1024 * rgb.shape[0] / rgb.shape[1]))), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def _crop_to(rgb, aspect, offset=0.5):
    """The largest crop of rgb with width/height = aspect, slid to `offset` (0..1) along the free axis."""
    h, w = rgb.shape[:2]
    if w / h > aspect:
        cw = int(round(h * aspect))
        x = int(round((w - cw) * offset))
        return rgb[:, x:x + cw]
    ch = int(round(w / aspect))
    y = int(round((h - ch) * offset))
    return rgb[y:y + ch]


def closeup(rgb, size, seg, det, lama, esr):
    """[(slab, alpha)] for a texture close-up (the stone fills the frame), or the reason it is not one. size =
    (length, width) of the product in metres, or None."""
    h, w = rgb.shape[:2]
    emb = proto.embed(seg, rgb)
    _, raw = proto.slab_mask(seg, rgb, (0, 0, w - 1, h - 1), emb, widest=True)
    dy, dx = int(C.CLOSEUP_INSET * h), int(C.CLOSEUP_INSET * w)
    if min(raw[dy].mean(), raw[h - 1 - dy].mean(), raw[:, dx].mean(), raw[:, w - 1 - dx].mean()) < C.CLOSEUP_BORDER:
        return "not a close-up: background shows along a side (a whole slab that was not found)"
    if _sharp(rgb) < C.SHARP_MIN:
        return "blurred close-up"
    straps = openbook._straps(seg, det, rgb, (0, 0, w - 1, h - 1), emb)    # kept as shot (the user's choice)
    marks = proto.mark_mask(rgb) | proto.flare_mask(rgb, (straps == 0).astype(np.uint8)) | \
        (cv2.dilate(1 - raw, np.ones((15, 15), np.uint8)) & (straps == 0))
    rgb = proto.erase(rgb, marks, lama, raw)
    length, width = size or C.DEFAULT_SLAB
    stone = _crop_to(rgb, length / width)
    factor = C.OUT_W * (1 - 2 * C.MARGIN) / stone.shape[1]
    if factor > 1:
        stone = compose.upscale(stone, esr, factor)
    stone = proto.even_light(stone, np.ones(stone.shape[:2], np.uint8))
    return [(stone, np.ones(stone.shape[:2], np.float32))]
