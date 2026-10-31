"""Straps, sample cards, stickers and the photo's own rack taken off a photo with an editing model, safely. The model redraws
all it is given and can invent stone (it turned a black slab blue and gold), so: it is given only a close crop around each
mark found; only the found area is taken from its output, with its edges brought to the real stone's tone, the rest stays the
real photo; and the result is refused when the stone beside that area was repainted, or the filled area does not look like
the stone around it. Never guessed: a refused photo is reported.
"""
import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks, outline


def keep_on_slab(found, stone):
    """Only the parts of `found` that lie on the slab: a code painted on the floor or a sign behind is cropped away anyway.
    The slab is the stone's convex hull: a card or a rack in front of it is a hole or a notch in what was seen as stone."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(found)
    out = np.zeros_like(found)
    if not stone.any():
        return out
    slab = cv2.fillConvexPoly(np.zeros_like(found), cv2.convexHull(cv2.findNonZero(stone)), 1)
    for i in range(1, n):
        part = labels == i
        if (slab[part] > 0).mean() >= C.CARD_ON_SLAB:
            out[part] = 1
    return out


def find_marks(seg, det, rgb):
    """(mask of straps, mask of cards, stickers and labels on the slab, mask of the slab): what should come off this photo,
    and the stone that must not change."""
    h, w = rgb.shape[:2]
    emb = marks.embed(seg, rgb)
    boxes = [b for b in outline.detect(det, rgb) if (b[2] - b[0]) * (b[3] - b[1]) >= C.MIN_SLAB_SHARE * w * h]
    box = max(boxes, key=lambda b: b[4])[:4] if boxes else (0, 0, w - 1, h - 1)
    stone = marks.slab_mask(seg, rgb, box, emb, widest=True)[0]
    straps = outline._straps(seg, det, rgb, box, emb)
    cards = np.zeros((h, w), np.uint8)
    for x0, y0, x1, y1, _ in outline.detect(det, rgb, C.CARD_PROMPT, C.CARD_MIN_SCORE):
        if (x1 - x0) * (y1 - y0) > C.CARD_MAX_BOX * w * h:
            continue
        cards |= marks.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
    k = max(3, int(C.RETOUCH_GROW * max(h, w)))
    return straps, cv2.dilate(keep_on_slab(cards, stone), np.ones((k, k), np.uint8)), stone


def _lab(rgb):
    return cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)


def changed_outside(original, edited, mask, stone):
    """How much the broad colour of the STONE moved outside the found area (Lab, a high percentile, around its even drift):
    near zero when only the marks were edited, large when the model repainted the slab. An even drift of the whole crop is
    no repaint, _fit_tone takes it out; it has its own limit. The room behind the slab does not count: it is cut away."""
    k = max(3, int(C.RETOUCH_GUARD * max(mask.shape)))
    outside = (cv2.dilate(mask, np.ones((k, k), np.uint8)) == 0) & (stone > 0)
    if not outside.any():                              # the crop is all mark: nothing beside it to judge the model by
        return 0.0
    sigma = C.RETOUCH_BROAD * max(mask.shape)
    diff = (cv2.GaussianBlur(_lab(original), (0, 0), sigma) - cv2.GaussianBlur(_lab(edited), (0, 0), sigma))[outside]
    drift = np.median(diff, axis=0)
    if np.linalg.norm(drift) > C.RETOUCH_MAX_DRIFT:
        return float("inf")
    return float(np.percentile(np.linalg.norm(diff - drift, axis=1), C.RETOUCH_PCT))


def _fit_tone(original, edited, mask, stone):
    """The edit with its fill's edges brought to the real stone's tone just outside them, the correction carried smoothly
    across the fill: the fill keeps its own veining, but leaves no band or step. Where the fill's edge is off the stone
    (a strap sticking out above the slab) nothing is measured."""
    k = max(3, int(C.RETOUCH_TONE_BAND * max(mask.shape)))
    kernel = np.ones((k, k), np.uint8)
    outer = ((cv2.dilate(mask, kernel) > 0) & (mask == 0) & (stone > 0)).astype(np.float32)
    inner = ((mask > 0) & (cv2.erode(mask, kernel, borderValue=1) == 0)).astype(np.float32)
    if not outer.any() or not inner.any():
        return edited

    def tone(rgb, w):
        return cv2.GaussianBlur(rgb.astype(np.float32) * w[..., None], (0, 0), k) / np.maximum(cv2.GaussianBlur(w, (0, 0), k), 1e-3)[..., None]
    edge = ((cv2.dilate(mask, kernel) > 0) & (cv2.erode(mask, kernel, borderValue=1) == 0) & (cv2.GaussianBlur(outer, (0, 0), k) > 0.05)).astype(np.float32)
    if not edge.any():
        return edited
    step = marks._push_pull(tone(original, outer) - tone(edited, inner), edge)
    return np.clip(edited + cv2.GaussianBlur(step, (0, 0), k), 0, 255).astype(np.uint8)


def fill_unlike_surroundings(edited, mask):
    """How unlike the stone around it the fill is: the larger of its colour distance and its texture ratio, scaled so 1 is
    the limit. An invented pile of rocks where a card lay fails this, so does a smooth smear; stone continuing under a strap
    does not."""
    k = max(3, int(C.RETOUCH_RING * max(mask.shape)))
    ring = (cv2.dilate(mask, np.ones((k, k), np.uint8)) > 0) & (mask == 0)
    if not ring.any() or not mask.any():
        return float("inf")
    lab = _lab(edited)
    colour = float(np.linalg.norm(lab[mask > 0].mean(axis=0) - lab[ring].mean(axis=0)))
    detail = np.abs(cv2.Laplacian(lab[:, :, 0], cv2.CV_32F))
    texture = float((detail[mask > 0].mean() + 0.5) / (detail[ring].mean() + 0.5))
    return max(colour / C.RETOUCH_MAX_COLOUR, texture / C.RETOUCH_MAX_TEXTURE, 1 / texture / C.RETOUCH_MAX_TEXTURE)


def merge(original, edited, mask, stone):
    """(the real photo with only the found area taken from the edit, why it was refused or "")."""
    h, w = original.shape[:2]
    edited = cv2.resize(edited, (w, h), interpolation=cv2.INTER_CUBIC)
    moved = changed_outside(original, edited, mask, stone)
    if moved > C.RETOUCH_MAX_CHANGE:
        return None, f"retouch: the model changed the stone ({moved:.1f})"
    edited = _fit_tone(original, edited, mask, stone)
    unlike = fill_unlike_surroundings(edited, mask)
    if unlike > 1:
        return None, f"retouch: the fill does not match the stone around it ({unlike:.2f})"
    k = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), C.RETOUCH_FEATHER)[..., None]
    return (original * (1 - k) + edited * k).clip(0, 255).astype(np.uint8), ""


def regions(mask):
    """The crops (x0, y0, x1, y1) to edit: one around each group of marks, with stone on all sides for the model to continue
    and never so narrow that it sees only the mark."""
    h, w = mask.shape
    pad = max(3, int(C.RETOUCH_MARGIN * max(h, w)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(mask, np.ones((2 * pad + 1, 2 * pad + 1), np.uint8)))
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        need_w = max(bw, C.RETOUCH_MIN_CROP, int(bh / C.RETOUCH_MAX_ASPECT + 1))
        need_h = max(bh, C.RETOUCH_MIN_CROP, int(bw / C.RETOUCH_MAX_ASPECT + 1))
        x0, y0 = max(0, x + bw // 2 - need_w // 2), max(0, y + bh // 2 - need_h // 2)
        boxes.append((x0, y0, min(w, x0 + need_w), min(h, y0 + need_h)))
    return sorted(boxes)


def model_size(w, h):
    """(width, height) a crop is edited at: about EDIT_PIXELS, a small crop enlarged at most RETOUCH_MAX_SCALE, in the
    multiples of 16 the model needs."""
    k = min(C.RETOUCH_MAX_SCALE, (C.EDIT_PIXELS / (w * h)) ** 0.5)
    return max(16, int(w * k) // 16 * 16), max(16, int(h * k) // 16 * 16)


def retouch(rgb, mask, stone, edit):
    """(rgb with every mark taken off, why it was refused or ""): edit(crop) is the model. One refused mark refuses the photo:
    half a retouch (one strap off, one on) is worse than none."""
    out = rgb.copy()
    for x0, y0, x1, y1 in regions(mask):
        piece, reason = merge(rgb[y0:y1, x0:x1], edit(rgb[y0:y1, x0:x1]), mask[y0:y1, x0:x1], stone[y0:y1, x0:x1])
        if piece is None:
            return None, reason
        out[y0:y1, x0:x1] = piece
    return out, ""
