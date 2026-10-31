"""Straps, sample cards, stickers and the photo's own rack taken off a photo with an editing model, safely. The model redraws
all it is given and can invent stone (it turned a black slab blue and gold), so: it is given only a close crop around each
mark found; only the found area is taken from its output, with its broad tone taken from the real stone, the rest stays the
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


def keep_on_slab(found, slab, share):
    """Only the parts of `found` with this share on `slab`: a code painted on the floor, a sign behind or the edge of the
    next slab in the row is cropped away anyway."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(found)
    out = np.zeros_like(found)
    for i in range(1, n):
        part = labels == i
        if (slab[part] > 0).mean() >= share:
            out[part] = 1
    return out


def same_ink(rgb, found, slab, long):
    """More of the writing the detector found part of: near each found mark on the slab, strokes in the same vivid ink
    (the numbers under a block's code in the same marker). Only where the ink is vivid, of a colour stone does not have
    and unlike this stone, so writing on a blue stone, a white paper label or an orange clamp adds nothing."""
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    ab = cv2.GaussianBlur(lab[:, :, 1:], (0, 0), 1.0)
    vivid = np.hypot(ab[..., 0], ab[..., 1]) > C.INK_CHROMA
    stone = (slab > 0) & (found == 0)
    more = np.zeros_like(found)
    if not stone.any():
        return more
    around = np.median(ab[stone], axis=0)
    k = max(1, int(C.INK_REACH * long))
    n, parts = cv2.connectedComponents(found)
    for i in range(1, n):
        part = parts == i
        ink = part & vivid
        if ink.sum() < C.INK_MIN_SHARE * part.sum():
            continue
        colour = np.median(ab[ink], axis=0)
        # Blue, green, pink and violet ink only: yellow, orange and red-brown are also the stone's own iron colours.
        if colour[1] > C.INK_MAX_YELLOW or np.linalg.norm(colour - around) < C.INK_APART:
            continue
        near = (cv2.dilate(part.astype(np.uint8), np.ones((2 * k + 1, 2 * k + 1), np.uint8)) > 0) & stone
        more |= (near & vivid & (np.linalg.norm(ab - colour, axis=2) < C.INK_SAME)).astype(np.uint8)
    # A written character is a stroke of some size; a speck of the same colour is a crystal of the stone.
    n, pieces, stats, _ = cv2.connectedComponentsWithStats(more)
    small = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] < C.INK_MIN_PIECE * more.size]
    more[np.isin(pieces, small)] = 0
    return more


def find_marks(seg, det, rgb):
    """(straps, cards, stone, slab): masks of the straps and of the cards, stickers, labels and racks on the slab, what the
    segmenter saw as stone (it must not change), and the slab: the stone's outline and the detector's box around it (the
    segmenter can lose the strip of stone beyond a strap at the slab's edge, the box does not)."""
    h, w = rgb.shape[:2]
    emb = marks.embed(seg, rgb)
    boxes = [b for b in outline.detect(det, rgb) if (b[2] - b[0]) * (b[3] - b[1]) >= C.MIN_SLAB_SHARE * w * h]
    box = max(boxes, key=lambda b: b[4])[:4] if boxes else (0, 0, w - 1, h - 1)
    stone = marks.slab_mask(seg, rgb, box, emb, widest=True)[0]
    none = np.zeros((h, w), np.uint8)
    if not stone.any():                                # no slab seen: nothing on it to take off
        return none, none, stone, none
    hull = _slab(stone)
    slab = hull.copy()
    bx0, by0, bx1, by1 = (int(round(v)) for v in box)
    slab[by0:by1 + 1, bx0:bx1 + 1] = 1
    # A strap counts by the slab, box included. Cards, stickers and racks count by the stone's outline only: the box also
    # holds the floor and the hall beside the slab (a bucket, a sign).
    straps = keep_on_slab(outline._straps(seg, det, rgb, box, emb), slab, C.STRAP_ON_SLAB)
    cards = none.copy()
    for x0, y0, x1, y1, _ in outline.detect(det, rgb, C.CARD_PROMPT, C.CARD_MIN_SCORE):
        if (x1 - x0) * (y1 - y0) > C.CARD_MAX_BOX * w * h:
            continue
        cards |= marks.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
    cards = keep_on_slab(cards, hull, C.CARD_ON_SLAB)
    cards |= same_ink(rgb, cards, hull, max(h, w))
    k = max(3, int(C.RETOUCH_GROW * max(h, w)))
    return straps, cv2.dilate(cards, np.ones((k, k), np.uint8)), stone, slab


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


def fill_unlike_stone(original, edited, mask, stone, slab, long):
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
    inside = (mask > 0) & (cv2.erode(slab, inset, borderValue=1) > 0)
    if not ring.any() or not inside.any():
        return float("inf")
    shot, fill = _lab(original), _lab(edited)
    colour = float(np.linalg.norm(fill[inside].mean(axis=0) - shot[ring].mean(axis=0)))
    # Medians: text or a vein left beside the mark is a few strong strokes, it must not set the stone's texture.
    texture = float((np.median(np.abs(cv2.Laplacian(fill[:, :, 0], cv2.CV_32F))[inside]) + 0.5)
                    / (np.median(np.abs(cv2.Laplacian(shot[:, :, 0], cv2.CV_32F))[ring]) + 0.5))
    return max(colour / C.RETOUCH_MAX_COLOUR, texture / C.RETOUCH_MAX_BUSIER, 1 / texture / C.RETOUCH_MAX_SMOOTHER)


def too_wide(mask, long):
    """How far the fill would reach from the real stone around it (share of the long side): the model invents what it
    cannot see, more the further in it is. A strap or a label is filled from both sides; a fan of sample cards on a
    close-up is not, the model draws a loose rock where it lay."""
    return float(cv2.distanceTransform(mask, cv2.DIST_L2, 5).max()) / long


def without_shadow(original, edited, mask, stone, slab, long):
    """The photo with the shadow a mark cast lifted off the real stone: next to the mark on the slab, connected to it, where
    the edit is lighter than the photo beyond its own shift away from the mark and the colour did not move, the photo's
    lightness is raised by that much. The stone under a shadow stays the photo's own, and a shadow's soft edge fades out
    with it. A card or a rack the detector missed changes the colour too, and is left for a person to see."""
    k = max(1, int(C.SHADOW_REACH * long))
    near = (cv2.dilate(mask, np.ones((2 * k + 1, 2 * k + 1), np.uint8)) > 0) & (mask == 0) & (slab > 0)
    g = max(3, int(C.RETOUCH_GUARD * long))
    away = (cv2.dilate(mask, np.ones((2 * k + g, 2 * k + g), np.uint8)) == 0) & (stone > 0)
    if not near.any() or not away.any():
        return original
    shot, edit = _lab(original), _lab(edited)
    lift = cv2.GaussianBlur(edit[:, :, 0] - shot[:, :, 0], (0, 0), 1.5)
    lift -= np.median(lift[away])
    hue = np.linalg.norm(cv2.GaussianBlur(edit[:, :, 1:] - shot[:, :, 1:], (0, 0), 1.5), axis=2)
    shade = near & (lift > C.SHADOW_MIN_LIFT) & (hue < C.SHADOW_MAX_HUE)
    _, labels = cv2.connectedComponents((shade | (mask > 0)).astype(np.uint8))
    touching = np.unique(labels[mask > 0])
    shadow = (np.isin(labels, touching[touching > 0]) & shade).astype(np.float32)
    if not shadow.any():
        return original
    weight = cv2.GaussianBlur(shadow, (0, 0), C.RETOUCH_FEATHER)
    shot[:, :, 0] += weight * np.clip(lift, 0, None)
    return (np.clip(cv2.cvtColor(shot, cv2.COLOR_LAB2RGB), 0, 1) * 255 + 0.5).astype(np.uint8)


def merge(original, edited, mask, stone, slab, long):
    """(the real photo with only the found area taken from the edit, why it was refused or ""): long is the whole photo's
    long side, so every check means the same on a close crop as on a wide one."""
    h, w = original.shape[:2]
    edited = cv2.resize(edited, (w, h), interpolation=cv2.INTER_CUBIC)
    moved = recoloured(original, edited, mask, stone, long)
    if moved > C.RETOUCH_MAX_DRIFT:
        return None, f"retouch: the model recoloured the stone ({moved:.1f})"
    base = without_shadow(original, edited, mask, stone, slab, long)
    # The mark is replaced whole and the edit fades out beyond it, over real stone: a fade inside it would leave half of the
    # mark's own border, and a short one a straight seam where the fill's grain meets the stone's.
    b = max(1, int(C.RETOUCH_BLEND * long))
    reach = cv2.dilate(mask, np.ones((2 * b + 1, 2 * b + 1), np.uint8))
    # Matched and judged on the slab only: a label's part above the slab's top edge is the hall in the edit, and is cut away.
    # The fill's broad tone is the real stone's around it, carried in smoothly; the model gives only the finer texture. The
    # band it fades out over is matched on its own, to the stone beyond it, so the two do not average each other's tone.
    sigma = C.RETOUCH_TONE_SIGMA * long
    on_slab = (mask > 0) & (slab > 0)
    band = (reach > 0) & (mask == 0)
    fill = marks.match_tone(np.where(mask[..., None] > 0, edited, base), on_slab, ((mask == 0) & (stone > 0)).astype(np.float32), sigma)
    faded = marks.match_tone(np.where(band[..., None], edited, base), band & (slab > 0), ((reach == 0) & (stone > 0)).astype(np.float32), sigma)
    fill = np.where(band[..., None], faded, fill)
    unlike = fill_unlike_stone(base, fill, on_slab.astype(np.uint8), stone, slab, long)
    if unlike > 1:
        return None, f"retouch: the fill does not match the stone around it ({unlike:.2f})"
    k = np.clip(1 - cv2.distanceTransform((mask == 0).astype(np.uint8), cv2.DIST_L2, 5) / b, 0, 1)[..., None]
    return (base * (1 - k) + fill * k).clip(0, 255).astype(np.uint8), ""


def _fill(rgb, mask, stone, slab, box, edit):
    """(this crop with its marks filled, why it was refused or ""): a mark too wide to fill honestly never goes to the model.
    One draw of the model can recolour the stone where the next is faithful: each seed of C.RETOUCH_SEEDS is tried in turn
    (fixed, so the same photo gives the same result) and the first that passes every check is taken."""
    x0, y0, x1, y1 = box
    long = max(rgb.shape[:2])
    reach = too_wide(mask[y0:y1, x0:x1], long)
    if reach > C.RETOUCH_MAX_REACH:
        return None, f"retouch: the mark is too large to fill without inventing stone ({reach:.3f})"
    crop = rgb[y0:y1, x0:x1]
    for seed in C.RETOUCH_SEEDS:
        piece, reason = merge(crop, edit(crop, seed), mask[y0:y1, x0:x1], stone[y0:y1, x0:x1], slab[y0:y1, x0:x1], long)
        if piece is not None:
            return piece, ""
    return None, f"{reason} on all {len(C.RETOUCH_SEEDS)} tries"


def too_wide_to_fill(mask):
    """Whether any group of marks reaches too far from real stone to be filled without inventing what was under it."""
    long = max(mask.shape)
    return any(too_wide(mask[y0:y1, x0:x1], long) > C.RETOUCH_MAX_REACH for x0, y0, x1, y1 in regions(mask))


def clear_crop(mask):
    """(x0, y0, x1, y1) of the largest landscape part of the photo clear of every mark, at least CROP_MIN_SHARE of it, or
    None: a close-up whose fan of sample cards is too wide to fill is shown as the real stone beside the cards, never with
    stone invented under them. A margin keeps it off the marks and off a card the detector did not find beside them."""
    h, w = mask.shape
    s = C.CROP_WORK / max(h, w)
    small = cv2.resize(mask, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_NEAREST)
    k = max(1, round(C.CROP_MARGIN * C.CROP_WORK))
    free = cv2.dilate(small, np.ones((2 * k + 1, 2 * k + 1), np.uint8)) == 0
    lo, hi = C.CROP_ASPECT
    rows, cols = free.shape
    heights = np.zeros(cols + 1, int)
    best, box = 0, None
    # Every maximal clear rectangle, by the histogram of clear run lengths per row; each is cut to the allowed shape.
    for y in range(rows):
        heights[:cols] = np.where(free[y], heights[:cols] + 1, 0)
        stack = []
        for x in range(cols + 1):
            start = x
            while stack and stack[-1][1] >= heights[x]:
                sx, sh = stack.pop()
                bw = x - sx
                cw = min(bw, int(sh * hi))
                ch = min(sh, int(cw / lo))
                if cw * ch > best:
                    best, box = cw * ch, (sx, y - sh + 1, sx + cw, y - sh + 1 + ch)
                start = sx
            stack.append((start, heights[x]))
    if box is None or best < C.CROP_MIN_SHARE * rows * cols:
        return None
    x0, y0, x1, y1 = (round(v / s) for v in box)
    return x0, y0, min(w, x1), min(h, y1)


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


def retouch(rgb, mask, stone, slab, edit):
    """(rgb with every mark taken off, why it was refused or ""): edit(crop, seed) is the model. One refused mark refuses the photo:
    half a retouch (one strap off, one on) is worse than none."""
    out = rgb.copy()
    for x0, y0, x1, y1 in regions(mask):
        piece, reason = _fill(rgb, mask, stone, slab, (x0, y0, x1, y1), edit)
        if piece is None:
            return None, reason
        out[y0:y1, x0:x1] = piece
    return out, ""
