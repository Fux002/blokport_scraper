"""Straps, sample cards, stickers and the photo's own rack taken off a photo with an editing model, safely. The model redraws
all it is given and can invent stone (it turned a black slab blue and gold), so: it is given only a close crop around each
mark found; only the found area is taken from its output, with its edges brought to the real stone's tone, the rest stays the
real photo; and the result is refused when the model recoloured the stone beside that area, or the filled area does not look
like the real stone around it, or the mark is too wide to fill without inventing what was under it. Never guessed: a refused
photo is reported.
"""
import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks, outline


def _slab(stone):
    """The slab: the stone's convex hull, so a card or a rack in front of it, a hole or a notch in what was seen as stone,
    counts as on it."""
    return cv2.fillConvexPoly(np.zeros_like(stone), cv2.convexHull(cv2.findNonZero(stone)), 1)


def keep_on_slab(found, stone, share):
    """Only the parts of `found` with this share on the slab: a code painted on the floor, a sign behind or the edge of the
    next slab in the row is cropped away anyway."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(found)
    out = np.zeros_like(found)
    if not stone.any():
        return out
    slab = _slab(stone)
    for i in range(1, n):
        part = labels == i
        if (slab[part] > 0).mean() >= share:
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
    straps = keep_on_slab(outline._straps(seg, det, rgb, box, emb), stone, C.STRAP_ON_SLAB)
    cards = np.zeros((h, w), np.uint8)
    for x0, y0, x1, y1, _ in outline.detect(det, rgb, C.CARD_PROMPT, C.CARD_MIN_SCORE):
        if (x1 - x0) * (y1 - y0) > C.CARD_MAX_BOX * w * h:
            continue
        cards |= marks.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
    k = max(3, int(C.RETOUCH_GROW * max(h, w)))
    return straps, cv2.dilate(keep_on_slab(cards, stone, C.CARD_ON_SLAB), np.ones((k, k), np.uint8)), stone


def _lab(rgb):
    return cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)


def recoloured(original, edited, mask, stone, long):
    """How far the model evenly moved the broad colour of the real stone around the found area (Lab): near zero when it
    only took the marks off, large when it drew another stone. Only the even shift counts: what it changed locally beside
    the marks (it also takes off logos and writing nobody marked) is never pasted. The slab's own edge and the room behind
    it do not count: the model redraws them freely and they are cut away."""
    k = max(3, int(C.RETOUCH_GUARD * long))
    kernel = np.ones((k, k), np.uint8)
    outside = (cv2.dilate(mask, kernel) == 0) & (cv2.erode(stone, kernel, borderValue=1) > 0)
    if not outside.any():                              # the crop is all mark: nothing beside it to judge the model by
        return 0.0
    sigma = C.RETOUCH_BROAD * long
    diff = (cv2.GaussianBlur(_lab(original), (0, 0), sigma) - cv2.GaussianBlur(_lab(edited), (0, 0), sigma))[outside]
    return float(np.linalg.norm(np.median(diff, axis=0)))


def _fit_tone(original, edited, mask, stone, long):
    """The edit with its fill's edges brought to the real stone's tone just outside them, the correction carried smoothly
    across the fill: the fill keeps its own veining, but leaves no band or step. Where the fill's edge is off the stone
    (a strap sticking out above the slab) nothing is measured."""
    k = max(3, int(C.RETOUCH_TONE_BAND * long))
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


def fill_unlike_stone(original, edited, mask, stone, long):
    """How unlike the REAL stone around it the fill is: the largest of its colour distance, how much busier and how much
    smoother it is, each scaled so 1 is the limit. Judged against the photo, not the edit: a model that drew another stone
    everywhere would match its own drawing. An invented pile of rocks where a card lay fails this, so does a smooth smear;
    stone continuing under a strap does not."""
    k = max(3, int(C.RETOUCH_RING * long))
    if not stone.any():
        return float("inf")
    # Both kept a few px inside the slab, and the ring off the mark: the slab's edge against the hall and the mark's edge in
    # the photo are no texture of the stone.
    inset = np.ones((2 * C.RETOUCH_RING_INSET + 1,) * 2, np.uint8)
    ring = (cv2.dilate(mask, np.ones((k, k), np.uint8)) > 0) & (cv2.dilate(mask, inset) == 0) & (cv2.erode(stone, inset, borderValue=1) > 0)
    inside = (mask > 0) & (cv2.erode(_slab(stone), inset, borderValue=1) > 0)
    if not ring.any() or not inside.any():
        return float("inf")
    shot, fill = _lab(original), _lab(edited)
    colour = float(np.linalg.norm(fill[inside].mean(axis=0) - shot[ring].mean(axis=0)))
    texture = float((np.abs(cv2.Laplacian(fill[:, :, 0], cv2.CV_32F))[inside].mean() + 0.5)
                    / (np.abs(cv2.Laplacian(shot[:, :, 0], cv2.CV_32F))[ring].mean() + 0.5))
    return max(colour / C.RETOUCH_MAX_COLOUR, texture / C.RETOUCH_MAX_BUSIER, 1 / texture / C.RETOUCH_MAX_SMOOTHER)


def too_wide(mask, long):
    """How far the fill would reach from the real stone around it (share of the long side): the model invents what it
    cannot see, more the further in it is. A strap or a label is filled from both sides; a fan of sample cards on a
    close-up is not, the model draws a loose rock where it lay."""
    return float(cv2.distanceTransform(mask, cv2.DIST_L2, 5).max()) / long


def with_shadow(original, edited, mask, stone, long):
    """The found area with the shadow its mark cast: next to it on the slab, where the photo is darker than the edit by more
    than the edit's own shift away from the mark, with the same colour (a shadow only darkens the stone; a card or a rack the
    detector missed changes the colour too, and is left for a person to see rather than half taken off)."""
    k = max(1, int(C.SHADOW_REACH * long))
    near = (cv2.dilate(mask, np.ones((2 * k + 1, 2 * k + 1), np.uint8)) > 0) & (mask == 0) & (_slab(stone) > 0)
    g = max(3, int(C.RETOUCH_GUARD * long))
    away = (cv2.dilate(mask, np.ones((2 * k + g, 2 * k + g), np.uint8)) == 0) & (stone > 0)
    if not near.any() or not away.any():
        return mask
    shot, edit = _lab(original), _lab(edited)
    lift = cv2.GaussianBlur(edit[:, :, 0] - shot[:, :, 0], (0, 0), 1.5)
    hue = np.linalg.norm(cv2.GaussianBlur(edit[:, :, 1:] - shot[:, :, 1:], (0, 0), 1.5), axis=2)
    shade = near & (lift > np.median(lift[away]) + C.SHADOW_MIN_LIFT) & (hue < C.SHADOW_MAX_HUE)
    _, labels = cv2.connectedComponents((shade | (mask > 0)).astype(np.uint8))
    touching = np.unique(labels[mask > 0])
    return mask | (np.isin(labels, touching[touching > 0]) & shade).astype(np.uint8)


def merge(original, edited, mask, stone, long):
    """(the real photo with only the found area taken from the edit, why it was refused or ""): long is the whole photo's
    long side, so every check means the same on a close crop as on a wide one."""
    h, w = original.shape[:2]
    edited = cv2.resize(edited, (w, h), interpolation=cv2.INTER_CUBIC)
    moved = recoloured(original, edited, mask, stone, long)
    if moved > C.RETOUCH_MAX_DRIFT:
        return None, f"retouch: the model recoloured the stone ({moved:.1f})"
    # Matched and judged on the slab only: a label's part above the slab's top edge is the hall in the edit, and is cut away.
    on_slab = mask
    if stone.any():
        mask = with_shadow(original, edited, mask, stone, long)
        on_slab = mask & _slab(stone)
    edited = _fit_tone(original, edited, on_slab, stone, long)
    unlike = fill_unlike_stone(original, edited, on_slab, stone, long)
    if unlike > 1:
        return None, f"retouch: the fill does not match the stone around it ({unlike:.2f})"
    k = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), C.RETOUCH_FEATHER)[..., None]
    return (original * (1 - k) + edited * k).clip(0, 255).astype(np.uint8), ""


def _fill(rgb, mask, stone, box, edit):
    """(this crop with its marks filled, why it was refused or ""): a mark too wide to fill honestly never goes to the model."""
    x0, y0, x1, y1 = box
    long = max(rgb.shape[:2])
    reach = too_wide(mask[y0:y1, x0:x1], long)
    if reach > C.RETOUCH_MAX_REACH:
        return None, f"retouch: the mark is too large to fill without inventing stone ({reach:.3f})"
    return merge(rgb[y0:y1, x0:x1], edit(rgb[y0:y1, x0:x1]), mask[y0:y1, x0:x1], stone[y0:y1, x0:x1], long)


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
        piece, reason = _fill(rgb, mask, stone, (x0, y0, x1, y1), edit)
        if piece is None:
            return None, reason
        out[y0:y1, x0:x1] = piece
    return out, ""
