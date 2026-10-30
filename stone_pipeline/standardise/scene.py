"""Blocks and tiles keep their own scene (the user's choice): no composite, the photo as shot, cleaned and relit.

  block   the block found (detector + SAM) only to measure the light on it; lifting cables in front of it,
          printed labels and text overlays erased (LaMa); light corrected; upscaled to the standard width
  tile    tile photos show racks of cut pieces, no single tile: labels/overlays erased, light corrected over
          the whole photo, upscaled; never composited (that would invent a tile we cannot see)
Usage: python keep_scene.py <in_dir> <out_dir> <block|tile>
"""
from stone_pipeline.config.settings import STANDARDISE as C
import cv2
import numpy as np

from stone_pipeline.standardise import layout as compose
from stone_pipeline.standardise import outline as openbook
from stone_pipeline.standardise import marks as proto



def _cables(seg, det, rgb, emb, block_box):
    """Lifting cables/ropes crossing the block: detected boxes overlapping the block, SAM-masked, kept only
    when the mask is long and thin (a rope, not a stone face or a crane)."""
    h, w = rgb.shape[:2]
    bx0, by0, bx1, by1 = block_box
    out = np.zeros((h, w), np.uint8)
    for x0, y0, x1, y1, _ in openbook.detect(det, rgb, C.CABLE_PROMPT):
        if x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1:
            continue
        m = proto.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
        if not m.any() or m.sum() > C.CABLE_MAX_SHARE * h * w:
            continue
        (_, _), (a, b), _ = cv2.minAreaRect(cv2.findNonZero(m))
        if max(a, b) >= C.CABLE_ELONGATION * max(1.0, min(a, b)):
            out |= m
    return cv2.dilate(out, np.ones((9, 9), np.uint8))


def block(rgb, seg, det, lama, esr):
    """(processed photo, note) for a block photo."""
    h, w = rgb.shape[:2]
    boxes = [b for b in openbook.detect(det, rgb, C.BLOCK_PROMPT) if (b[2] - b[0]) * (b[3] - b[1]) >= C.BLOCK_MIN_SHARE * h * w]
    marks = proto.mark_mask(rgb)
    if boxes:
        box = max(boxes, key=lambda b: b[4])[:4]
        emb = proto.embed(seg, rgb)
        body = proto.slab_mask(seg, rgb, box, emb)[0]
        marks |= _cables(seg, det, rgb, emb, box)
        note = "block found: cables + marks erased, lit on the block"
    else:
        body = np.ones((h, w), np.uint8)
        note = "no block found: marks erased, lit on the whole photo"
    out, cut = _finish(proto.erase(rgb, marks, lama, body), body, esr)
    return out, f"{note}; the canvas crop cuts {cut:.0%} of the block" if cut > C.CROP_NOTE_MIN else note


def tile(rgb, lama, esr):
    """(processed photo, note) for a tile photo."""
    whole = np.ones(rgb.shape[:2], np.uint8)
    return _finish(proto.erase(rgb, proto.mark_mask(rgb), lama, whole), whole, esr)[0], \
        "marks erased, lit on the whole photo"


def _canvas_crop(rgb, body):
    """(rgb, body) cropped to the canvas's shape, slid to keep the block centred, and the share of it cut off."""
    h, w = rgb.shape[:2]
    aspect = C.OUT_W / C.OUT_H
    ys, xs = np.nonzero(body)
    if w / h > aspect:
        cw = int(round(h * aspect))
        x = int(np.clip((xs.min() + xs.max()) / 2 - cw / 2, 0, w - cw))
        box = (0, h, x, x + cw)
    else:
        ch = int(round(w / aspect))
        y = int(np.clip((ys.min() + ys.max()) / 2 - ch / 2, 0, h - ch))
        box = (y, y + ch, 0, w)
    kept = body[box[0]:box[1], box[2]:box[3]]
    return rgb[box[0]:box[1], box[2]:box[3]], kept, 1 - kept.sum() / max(1, body.sum())


def _finish(rgb, body, esr):
    """(the standard canvas, share of the block its crop cut off): cropped to shape, light measured on `body`,
    then the standard size (upscaled by ESRGAN when the photo is smaller)."""
    rgb, body, cut = _canvas_crop(rgb, body)
    lit = proto.even_light(rgb, body)
    factor = C.OUT_W / lit.shape[1]
    if factor > 1:
        lit = compose.upscale(lit, esr, factor)
    return cv2.resize(lit, (C.OUT_W, C.OUT_H), interpolation=cv2.INTER_AREA), cut
