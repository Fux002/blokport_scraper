"""Straps, sample cards and stickers taken off a photo with an editing model, safely. The model redraws the whole image and
can invent stone (it turned a black slab blue and gold), so: it runs only on a photo with a mark found; only the found
area is taken from its output, the rest stays the real photo; and the result is refused when the stone outside that area
changed, or the filled area does not look like the stone around it. Never guessed: a refused photo is reported.
"""
import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks, outline


def find_marks(seg, det, rgb):
    """(mask of straps, mask of cards, stickers and labels): what should come off this photo."""
    h, w = rgb.shape[:2]
    emb = marks.embed(seg, rgb)
    boxes = [b for b in outline.detect(det, rgb) if (b[2] - b[0]) * (b[3] - b[1]) >= C.MIN_SLAB_SHARE * w * h]
    box = max(boxes, key=lambda b: b[4])[:4] if boxes else (0, 0, w - 1, h - 1)
    straps = outline._straps(seg, det, rgb, box, emb)
    cards = np.zeros((h, w), np.uint8)
    for x0, y0, x1, y1, _ in outline.detect(det, rgb, C.CARD_PROMPT, C.CARD_MIN_SCORE):
        if (x1 - x0) * (y1 - y0) > C.CARD_MAX_BOX * w * h:
            continue
        cards |= marks.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
    k = max(3, int(C.RETOUCH_GROW * max(h, w)))
    return straps, cv2.dilate(cards, np.ones((k, k), np.uint8))


def _lab(rgb):
    return cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)


def changed_outside(original, edited, mask):
    """How much the broad colour moved outside the found area (Lab, a high percentile): near zero when only the marks
    were edited, large when the model repainted the stone."""
    k = max(3, int(C.RETOUCH_GUARD * max(mask.shape)))
    outside = cv2.dilate(mask, np.ones((k, k), np.uint8)) == 0
    if not outside.any():
        return float("inf")
    sigma = C.RETOUCH_BROAD * max(mask.shape)
    diff = np.linalg.norm(cv2.GaussianBlur(_lab(original), (0, 0), sigma) - cv2.GaussianBlur(_lab(edited), (0, 0), sigma), axis=2)
    return float(np.percentile(diff[outside], C.RETOUCH_PCT))


def fill_unlike_surroundings(edited, mask):
    """How unlike the stone around it the fill is: the larger of its colour distance and its texture ratio, scaled so 1 is
    the limit. An invented pile of rocks where a card lay fails this; stone continuing under a strap does not."""
    k = max(3, int(C.RETOUCH_RING * max(mask.shape)))
    ring = (cv2.dilate(mask, np.ones((k, k), np.uint8)) > 0) & (mask == 0)
    if not ring.any() or not mask.any():
        return float("inf")
    lab = _lab(edited)
    colour = float(np.linalg.norm(lab[mask > 0].mean(axis=0) - lab[ring].mean(axis=0)))
    detail = np.abs(cv2.Laplacian(lab[:, :, 0], cv2.CV_32F))
    texture = float((detail[mask > 0].mean() + 0.5) / (detail[ring].mean() + 0.5))
    return max(colour / C.RETOUCH_MAX_COLOUR, texture / C.RETOUCH_MAX_TEXTURE)


def merge(original, edited, mask):
    """(the real photo with only the found area taken from the edit, why it was refused or "")."""
    h, w = original.shape[:2]
    edited = cv2.resize(edited, (w, h), interpolation=cv2.INTER_CUBIC)
    moved = changed_outside(original, edited, mask)
    if moved > C.RETOUCH_MAX_CHANGE:
        return None, f"retouch: the model changed the stone ({moved:.1f})"
    unlike = fill_unlike_surroundings(edited, mask)
    if unlike > 1:
        return None, f"retouch: the fill does not match the stone around it ({unlike:.2f})"
    k = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), C.RETOUCH_FEATHER)[..., None]
    return (original * (1 - k) + edited * k).clip(0, 255).astype(np.uint8), ""
