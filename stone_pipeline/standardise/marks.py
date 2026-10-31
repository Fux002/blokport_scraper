"""Shared steps of the slab standardiser (our own GPU stack, no paid API); compose.py lays the result out.

  segment      SAM, box-prompted with the detector's slab box; thin attachments (rails, clips) trimmed off
  outline      the slab's four straight edges, intersected (a chipped corner is a short fifth edge, ignored)
  erase        foreign objects inside the outline + OCR'd text widened to its card or logo, filled by LaMa
  light        flat-field + cast from near-grey stone + exposure lift + clarity
"""

from __future__ import annotations

from stone_pipeline.config.settings import STANDARDISE as C
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


# --- settings (one block) -------------------------------------------------------------------------------
# Light is corrected the way more or evener light would change the stone: gains in LINEAR light (brighter =
# proportionally more colourful, never paler), so the stone's own colours and darkness are kept.


def load_sam():
    """The slab segmenter: SAM, box-prompted (points picked single clasts in breccia; BEN2 left holes on large
    pale slabs and spilled into racks)."""
    import torch
    from transformers import SamModel, SamProcessor
    device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    dtype = torch.float16 if device == "cuda" else torch.float32     # fp16 on CUDA; MPS/CPU stay fp32
    sam = SamModel.from_pretrained(C.SAM_MODEL, revision=C.SAM_REVISION, torch_dtype=dtype).to(device).eval()
    return sam, SamProcessor.from_pretrained(C.SAM_MODEL, revision=C.SAM_REVISION), device


def embed(model, rgb: np.ndarray):
    """SAM's image encoding of rgb: the expensive half of SAM, computed ONCE per photo and reused by every
    box prompt on it (main slab, partner probes)."""
    import torch
    sam, proc, device = model
    pixels = proc(Image.fromarray(rgb), return_tensors="pt")["pixel_values"].to(device, sam.dtype)
    with torch.no_grad():
        return sam.get_image_embeddings(pixels)


def _largest(mask: np.ndarray) -> np.ndarray:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n <= 1:
        return np.zeros_like(mask)
    return (labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)


def slab_mask(model, rgb: np.ndarray, box, emb, widest=False) -> tuple[np.ndarray, np.ndarray]:
    """(the object inside box [x0, y0, x1, y1] (or the one object at a point (x, y)) with holes filled and
    attachments trimmed, SAM's own mask: what it counted as stone; the holes are where labels and clips sit).
    emb = embed(model, rgb). widest: SAM's
    largest proposal instead of its most confident one (with straps in front, the confident one is the part
    between the straps, the largest the whole slab; in a close-up, the whole frame of stone)."""
    import torch
    sam, proc, device = model
    if len(box) == 2:
        raw = proc(Image.fromarray(rgb), input_points=[[[[float(v) for v in box]]]], return_tensors="pt")
        prompt = {"input_points": raw["input_points"].to(device, sam.dtype)}
    else:
        raw = proc(Image.fromarray(rgb), input_boxes=[[[float(v) for v in box]]], return_tensors="pt")
        prompt = {"input_boxes": raw["input_boxes"].to(device, sam.dtype)}
    with torch.no_grad():
        out = sam(image_embeddings=emb, **prompt)
    masks = proc.image_processor.post_process_masks(out.pred_masks.float().cpu(), raw["original_sizes"],
                                                    raw["reshaped_input_sizes"])[0][0]
    pick = int(masks.flatten(1).sum(1).argmax()) if widest else int(out.iou_scores.cpu()[0][0].argmax())
    raw_mask = _largest(masks[pick].numpy().astype(np.uint8))
    contours, _ = cv2.findContours(raw_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = cv2.drawContours(np.zeros_like(raw_mask), contours, -1, 1, -1)   # the slab, holes filled
    k = max(3, int(C.TRIM_SHARE * min(mask.shape)))
    return _largest(cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))), raw_mask


def flare_mask(rgb: np.ndarray, on: np.ndarray) -> np.ndarray:
    """Reflections of lamps on a polished slab: blown out, colourless, far brighter than the stone around them, and
    compact. A white vein fails these (thin, irregular, not blown out, or on stone nearly as bright)."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    s = C.FLARE_BASE_SIZE / max(h, w)
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    base = cv2.resize(cv2.medianBlur(small, C.FLARE_BASE_BLUR), (w, h), interpolation=cv2.INTER_LINEAR)
    found = (hsv[:, :, 2] >= C.FLARE_MIN_V) & (hsv[:, :, 1] <= C.FLARE_MAX_S) & (gray.astype(np.int16) - base >= C.FLARE_CONTRAST) & (on > 0)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(found.astype(np.uint8))
    out = np.zeros((h, w), np.uint8)
    slab = max(1, int((on > 0).sum()))
    kept = 0
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if not C.FLARE_AREA[0] * slab <= area <= C.FLARE_AREA[1] * slab or min(bw, bh) < C.FLARE_MIN_THICK:
            continue
        part = (labels[y:y + bh, x:x + bw] == i).astype(np.uint8)
        contours, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        hull = cv2.contourArea(cv2.convexHull(max(contours, key=cv2.contourArea)))
        clipped = (hsv[y:y + bh, x:x + bw, 2][part > 0] >= C.FLARE_CLIP_V).mean()
        if area >= C.FLARE_SOLIDITY * max(1.0, hull) and clipped >= C.FLARE_CLIPPED:
            out[labels == i] = 1
            kept += 1
    if kept > C.FLARE_MAX_COUNT:                         # a pattern of white pieces is the stone, not lamps: never touched
        return np.zeros((h, w), np.uint8)
    return cv2.dilate(out, np.ones((C.FLARE_GROW, C.FLARE_GROW), np.uint8))


def foreign_objects(quad: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """Regions INSIDE the fitted slab outline that SAM did not count as stone (a printed label, a sticker, a
    clip; also when they touch the slab's edge), kept when object-sized: not a thin sliver along the edge
    (corner-fit noise) and not so large it is a dark region of the stone."""
    inside = cv2.fillConvexPoly(np.zeros_like(raw), quad.astype(np.int32), 1)
    holes = ((inside > 0) & (raw == 0)).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(holes)
    out = np.zeros_like(holes)
    area, short = inside.sum(), min(np.ptp(quad[:, 0]), np.ptp(quad[:, 1]))
    for i in range(1, n):
        _, _, bw, bh, a = stats[i]
        if C.FOREIGN_MIN * area <= a <= C.FOREIGN_MAX * area and min(bw, bh) >= C.FOREIGN_THICK * short:
            out[labels == i] = 1
    return cv2.dilate(out, np.ones((9, 9), np.uint8))


def _meet(p1, p2, p3, p4):
    """Intersection of line p1-p2 with line p3-p4 (None when parallel)."""
    d1, d2 = p2 - p1, p4 - p3
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    s = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / den
    return p1 + s * d1


def four_corners(mask: np.ndarray) -> np.ndarray | None:
    """The slab's corners [tl, tr, br, bl]: its four longest straight hull edges, extended and intersected, so a
    chipped corner (a short fifth edge) still yields the slab's rectangle. None when those four edges are not
    most of the outline (a rough natural edge, several pieces) or the shape is not slab-like."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    hull = cv2.convexHull(max(contours, key=cv2.contourArea))
    for eps in C.QUAD_EPS:
        poly = cv2.approxPolyDP(hull, eps * cv2.arcLength(hull, True), True).reshape(-1, 2).astype(np.float64)
        if len(poly) < 4:
            return None
        edges = [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
        lengths = np.array([np.linalg.norm(b - a) for a, b in edges])
        keep = sorted(np.argsort(lengths)[-4:])
        if lengths[keep].sum() >= C.EDGE_COVER_MIN * lengths.sum():
            break
    else:
        return None
    pts = [_meet(*edges[keep[i]], *edges[keep[(i + 1) % 4]]) for i in range(4)]
    if any(p is None for p in pts):
        return None
    pts = np.array(pts, np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    q = np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)
    if len({tuple(c) for c in q.tolist()}) < 4:
        return None
    sides = [np.linalg.norm(q[(i + 1) % 4] - q[i]) for i in range(4)]
    if max(sides[0], sides[2]) > C.SIDE_RATIO_MAX * min(sides[0], sides[2]) or \
            max(sides[1], sides[3]) > C.SIDE_RATIO_MAX * min(sides[1], sides[3]):
        return None                                           # not slab-shaped: a neighbour or prop was included
    top, left = q[1] - q[0], q[3] - q[0]
    tilt = max(abs(np.degrees(np.arctan2(top[1], top[0]))), abs(90 - abs(np.degrees(np.arctan2(left[1], left[0])))))
    return q if tilt <= C.MAX_TILT_DEG else None


def mark_mask(rgb: np.ndarray, on: np.ndarray | None = None) -> np.ndarray:
    """Text clusters (supplier labels, logo words, header overlays), each widened to its card/logo. `on`: only
    text lying on this region counts (a slab photo: text outside the slab is cropped away anyway, and widening
    it could only reach onto the stone); None = the whole photo."""
    h, w = rgb.shape[:2]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.png"
        Image.fromarray(rgb).save(p)
        tsv = subprocess.run(["tesseract", str(p), "stdout", "--psm", "11", "tsv"], capture_output=True,
                             text=True, timeout=180).stdout
    words, anchors = [], []
    for line in tsv.splitlines()[1:]:
        f = line.split("\t")
        if len(f) != 12 or f[10] in ("", "-1") or float(f[10]) < C.OCR_MIN_CONF:
            continue
        chars = len(re.sub(r"[^A-Za-z0-9]", "", f[11]))
        if chars >= 2:
            words.append(tuple(map(int, f[6:10])))
            if (float(f[10]) >= C.OCR_ANCHOR_CONF and chars >= C.OCR_ANCHOR_LEN) or float(f[10]) >= C.OCR_SHORT_CONF:
                anchors.append(words[-1])
    mask = np.zeros((h, w), np.uint8)
    if not anchors:
        return mask
    wh = int(np.median([b[3] for b in anchors]))           # text height from real words, not texture noise
    seed = np.zeros((h, w), np.uint8)
    for x, y, bw, bh in _on_anchor_lines(words, anchors):
        if on is None or on[y:y + bh, x:x + bw].mean() >= C.MARK_ON_MIN:
            cv2.rectangle(seed, (x, y), (x + bw, y + bh), 1, -1)
    if not seed.any():
        return mask
    joined = cv2.dilate(seed, np.ones((max(3, int(C.OCR_CLUSTER * wh)),) * 2, np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined)
    real = range(1, n)
    pad = min(int(C.MARK_PAD * wh), int(C.MARK_PAD_MAX * max(h, w)))
    # printed cards: bright, flat, rectangular components that hold a recognised word -> the whole card
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    bright = ((hsv[:, :, 2] > 215) & (hsv[:, :, 1] < 35)).astype(np.uint8)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((max(3, wh // 2),) * 2, np.uint8))
    bn, blab, bst, _ = cv2.connectedComponentsWithStats(bright)
    for j in range(1, bn):
        x, y, bw, bh, area = bst[j]
        if bw * bh <= C.CARD_MAX_SHARE * w * h and area / max(1, bw * bh) >= C.CARD_FILL and seed[y:y + bh, x:x + bw].any():
            cv2.rectangle(mask, (max(0, x - pad // 2), max(0, y - pad // 2)), (min(w, x + bw + pad // 2), min(h, y + bh + pad // 2)), 1, -1)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    for i in sorted(real):
        x, y, bw, bh = stats[i, :4]
        box = (max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad))
        card = _card_outline(gray, (x, y, x + bw, y + bh), wh)
        # A printed card is barely larger than its text; a rectangle far larger is something else (the slab's
        # own corner, a post), and erasing it would invent stone.
        if card and (card[2] - card[0]) * (card[3] - card[1]) <= C.CARD_MAX_TO_TEXT * bw * bh:
            box = (min(box[0], card[0]), min(box[1], card[1]), max(box[2], card[2]), max(box[3], card[3]))
        cv2.rectangle(mask, box[:2], box[2:], 1, -1)
    return mask


def _on_anchor_lines(words: list, anchors: list) -> list:
    """The surely-read words plus the rest of their text lines ("310X194 CM" beside "SIZE"), grown until nothing
    more joins: a word joins only on the rows of a kept word, near it and about its height. Stone texture that
    OCR misreads as low-confidence words sits on no such line, so it can never grow a label across the slab."""
    kept, rest = list(anchors), [b for b in words if b not in anchors]
    grew = True
    while grew:
        grew = False
        for b in list(rest):
            x, y, bw, bh = b
            for rx, ry, rw, rh in kept:
                same_rows = y < ry + rh and ry < y + bh
                gap = max(rx - (x + bw), x - (rx + rw), 0)
                if same_rows and gap <= C.OCR_LINE_GAP * rh and C.OCR_LINE_HEIGHT[0] * rh <= bh <= C.OCR_LINE_HEIGHT[1] * rh:
                    kept.append(b)
                    rest.remove(b)
                    grew = True
                    break
    return kept


def _card_outline(gray: np.ndarray, text_box: tuple, wh: int) -> tuple | None:
    """The smallest rectangular outline (a card's thin border) enclosing a text cluster, searched within a
    window around it; None when the text sits on no card (a logo word, an overlay)."""
    h, w = gray.shape
    r = int(C.CARD_SEARCH * wh)
    x0, y0, x1, y1 = text_box
    wx0, wy0, wx1, wy1 = max(0, x0 - r), max(0, y0 - r), min(w, x1 + r), min(h, y1 + r)
    edges = cv2.Canny(gray[wy0:wy1, wx0:wx1], 60, 160)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        bx, by, bw, bh = cv2.boundingRect(c)
        bx, by = bx + wx0, by + wy0
        encloses = bx <= x0 and by <= y0 and bx + bw >= x1 and by + bh >= y1
        rect_like = cv2.contourArea(cv2.convexHull(c)) >= 0.85 * bw * bh
        if encloses and rect_like and bw * bh <= C.CARD_MAX_SHARE * w * h and (best is None or bw * bh < best[4]):
            best = (bx - 3, by - 3, bx + bw + 3, by + bh + 3, bw * bh)
    return best[:4] if best else None


def erase(rgb: np.ndarray, mask: np.ndarray, lama, stone: np.ndarray) -> np.ndarray:
    """rgb with mask filled in by LaMa; where the fill lies on `stone`, its broad tone matches the stone around it."""
    if not mask.any():
        return rgb
    out = lama(Image.fromarray(rgb), Image.fromarray(mask * 255))
    hole = (mask > 0) & (stone > 0)
    around = ((mask == 0) & (stone > 0)).astype(np.float32)
    return match_tone(np.asarray(out.convert("RGB"))[: rgb.shape[0], : rgb.shape[1]], hole, around, C.FILL_DETAIL_SIGMA * max(mask.shape))


def match_tone(filled: np.ndarray, hole: np.ndarray, around: np.ndarray, sigma: float) -> np.ndarray:
    """A fill keeps the stone's texture but can get its tone wrong (LaMa: an erased strap on bright stone comes out
    darker, darkest in its middle, with a step at its edge; the editing model: shaded where nothing around measures it).
    Inside the hole, the tone coarser than sigma (px, per channel, in linear light) is the tone of the real stone
    `around`, smoothly interpolated across (push-pull); only the finer texture is the fill's."""
    if not hole.any() or not around.any():             # no stone around the fill to take a tone from
        return filled
    lin = _to_linear(filled)

    def local(w):                                      # mean of lin over the pixels weighted w, at sigma
        return cv2.GaussianBlur(lin * w[..., None], (0, 0), sigma) / \
            np.maximum(cv2.GaussianBlur(w, (0, 0), sigma), 1e-6)[..., None]
    target = _push_pull(local(around), around)
    gain = target / np.maximum(local(hole.astype(np.float32)), 1e-6)
    out = filled.copy()
    out[hole] = _to_srgb(lin * gain)[hole]
    return out


def _push_pull(values: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """values (H x W x C) kept where weight (0..1) is 1 and smoothly filled in where it is 0: averaged down a
    pyramid (by weight) until the gaps close, then interpolated back up and blended in by weight."""
    w = weight[..., None]
    if min(weight.shape) < 4:
        return np.broadcast_to((values * w).sum((0, 1)) / max(float(weight.sum()), 1e-6), values.shape)
    size = ((weight.shape[1] + 1) // 2, (weight.shape[0] + 1) // 2)
    small_w = cv2.resize(weight, size, interpolation=cv2.INTER_AREA)
    small_v = cv2.resize(values * w, size, interpolation=cv2.INTER_AREA) / np.maximum(small_w, 1e-6)[..., None]
    coarse = cv2.resize(_push_pull(small_v, small_w), weight.shape[::-1], interpolation=cv2.INTER_LINEAR)
    return w * values + (1 - w) * coarse


def _to_linear(rgb: np.ndarray) -> np.ndarray:
    s = rgb.astype(np.float32) / 255
    return np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def _to_srgb(lin: np.ndarray) -> np.ndarray:
    s = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(np.clip(lin, 0.0031308, None), 1 / 2.4) - 0.055)
    return np.clip(s * 255 + 0.5, 0, 255).astype(np.uint8)


def _l_star(y):
    """CIE L* (0-100) of linear luminance y."""
    return np.where(y > 216 / 24389, 116 * np.cbrt(y) - 16, y * 24389 / 27)


def _shoulder(lin: np.ndarray) -> np.ndarray:
    """Values above SHOULDER roll off smoothly towards 1 (highlights keep their detail and colour)."""
    room = 1 - C.SHOULDER
    return np.where(lin > C.SHOULDER, C.SHOULDER + room * (1 - np.exp(-np.maximum(lin - C.SHOULDER, 0) / room)), lin)


def even_light(rgb: np.ndarray, slab: np.ndarray) -> np.ndarray:
    """Studio light on the stone, faithful to its colour. In linear light: even out the illumination (one gain
    per pixel, all channels alike), neutralise the light's cast measured on near-grey stone (bounded per-channel
    gains), lift exposure so the brightest areas sit near white (one bounded gain, never down; a dark stone
    keeps its darkness), soft highlight roll-off. Then a little clarity on L* and vibrance that lifts dull colours only."""
    m = slab > 0
    luma = np.array([0.2126, 0.7152, 0.0722], np.float32)
    lin = _to_linear(rgb)
    y = lin @ luma
    illum = cv2.GaussianBlur(y, (0, 0), C.FLAT_SIGMA * max(y.shape))
    lin *= (np.clip(float(illum[m].mean()) / np.maximum(illum, 1e-4), 0.75, 1.4) ** C.FLAT_STRENGTH)[:, :, None]

    lab = cv2.cvtColor(_to_srgb(lin).astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    neutral = (np.hypot(lab[:, :, 1], lab[:, :, 2]) <= C.WB_NEUTRAL_CHROMA) & m
    if neutral.sum() >= C.WB_MIN_NEUTRAL * m.sum():
        ref = neutral & (lab[:, :, 0] >= np.percentile(lab[:, :, 0][neutral], C.WHITE_PCT))
        means = lin[ref].mean(axis=0)
        lin *= np.clip((means.mean() / np.maximum(means, 1e-4)) ** C.WB_STRENGTH, 1 - C.WB_MAX, 1 + C.WB_MAX).astype(np.float32)

    bright = float(np.percentile((lin @ luma)[m], C.WHITE_PCT))
    if _l_star(bright) >= C.DARK_STONE_L:
        target = ((C.WHITE_TARGET_L + 16) / 116) ** 3
        lin *= float(np.clip(target / bright, 1.0, C.EXPOSURE_MAX))     # lift only: a bright photo is never dimmed
    lin = _shoulder(lin)

    lab = cv2.cvtColor(_to_srgb(lin).astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    L = lab[:, :, 0]
    lab[:, :, 0] = np.clip(L + C.CLARITY * (L - cv2.GaussianBlur(L, (0, 0), C.CLARITY_SIGMA * max(L.shape))), 0, 100)
    chroma = np.hypot(lab[:, :, 1], lab[:, :, 2])
    lab[:, :, 1:] *= (1 + C.VIBRANCE * np.clip(1 - chroma / C.VIBRANCE_FULL, 0, 1))[:, :, None]
    return np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB) * 255 + 0.5, 0, 255).astype(np.uint8)
