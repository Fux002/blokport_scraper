"""Open books: find the slabs in a photo (one, or a side-by-side pair) and lay a pair out as two squared slabs.

The outline is read for joints (straight gaps) along its width and its height: where it runs to the frame edge, a
joint near that end cuts off what SAM ran on into, a neighbour or the stand (no joint: the slab is cut off, no
composite). A pair is recognised two ways:
  * the outline covers BOTH slabs: a joint splits it into two equal halves (on an outline narrower than two
    slabs the halves must also mirror) -> split there;
  * the outline covers ONE slab: SAM is boxed into the slab-sized region past each side edge; an equal slab
    meeting it there whose veining mirrors across the joint is its partner.
A photo is composited only when every slab is whole: inside the frame, and the detector's box for it not
reaching past our outline.
"""

from __future__ import annotations

from stone_pipeline.config.settings import STANDARDISE as C
import cv2
import numpy as np
from PIL import Image
from stone_pipeline.standardise import marks as proto

# --- settings (one block) -------------------------------------------------------------------------------


def _size(q):
    tl, tr, br, bl = q
    return (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2, (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2


def _flat(rgb, q):
    w, h = _size(q)
    H = cv2.getPerspectiveTransform(q, np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32))
    return cv2.warpPerspective(rgb, H, (int(w), int(h)), flags=cv2.INTER_CUBIC), H


def _joint_profile(flat):
    """Per-column joint score along the squared width (1024 columns): a straight full-height gap shows as edge
    change in nearly every row plus a darker column, each as a robust z-score against its neighbourhood."""
    gray = cv2.cvtColor(flat, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (1024, max(64, int(1024 * gray.shape[0] / gray.shape[1])))).astype(np.float32)
    g = cv2.GaussianBlur(small, (0, 0), 1.0)
    change = np.median(np.abs(np.diff(g, axis=1)), axis=0)
    dark = np.median((g[:, 1:] + g[:, :-1]) / 2, axis=0)
    w = len(change)

    def z(v, sign):
        base = cv2.GaussianBlur(v.reshape(1, -1), (0, 0), w / 60).ravel()
        return sign * (v - base) / (np.median(np.abs(v - np.median(v))) * 1.4826 + 1e-6)
    return z(change, 1) + z(dark, -1)


def _strongest(score, lo, hi, minimum):
    """Share of the strongest joint scoring at least `minimum` between shares lo and hi, or None."""
    a, b = int(lo * len(score)), int(hi * len(score))
    i = a + int(np.argmax(score[a:b]))
    return i / len(score) if score[i] >= minimum else None


def _joints(score, lo, hi, minimum):
    """Shares where joints scoring at least `minimum` run between shares lo and hi, one per JOINT_NMS."""
    w = len(score)
    a, b = int(lo * w), int(hi * w)
    found = []
    for i in np.argsort(score[a:b])[::-1] + a:
        if score[i] < minimum:
            break
        if all(abs(i - j) > C.JOINT_NMS * w for j in found):
            found.append(int(i))
    return sorted(j / w for j in found)


def _halves_mirror(flat, s):
    """Do the veining's two sides of share s mirror each other (a bookmatch, not a straight crack)?"""
    gray = cv2.cvtColor(flat, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (512, max(64, int(512 * gray.shape[0] / gray.shape[1])))).astype(np.float32)
    i = int(s * 512)
    d = cv2.GaussianBlur(small, (0, 0), 1.0) - cv2.GaussianBlur(small, (0, 0), 6.0)
    d /= cv2.GaussianBlur(np.abs(d), (0, 0), 15) + 2.0
    half = min(i, 512 - i) - 3
    return _corr(d[:, i - half:i], d[:, i + 1:i + 1 + half][:, ::-1]) >= C.MIRROR_MIN


def _cut(q, flat, H, shares, rows):
    """The quads between consecutive column shares of the squared outline, between its row shares
    rows = (top, bottom), mapped back into the photo."""
    inv = np.linalg.inv(H)
    h, w = flat.shape[:2]
    top, bottom = rows[0] * (h - 1), rows[1] * (h - 1)
    cols = [cv2.perspectiveTransform(np.array([[[s * (w - 1), top], [s * (w - 1), bottom]]], np.float32), inv)[0]
            for s in shares]
    return [np.array([a[0], b[0], b[1], a[1]], np.float32) for a, b in zip(cols, cols[1:])]


def _ends(score, lo_at_frame, hi_at_frame):
    """(lo, hi): the shares of the outline that are slab along one axis, or None when the slab is cut off. An
    end at the frame edge is either a cut-off slab or SAM's mask running on into something past it (a
    neighbour, the stand under the slab): its edge is a physical joint, the STRONGEST one near that end (weaker
    bar: a slab against a pale post; a weaker vein further in would cut real stone off); no joint = cut off."""
    lo = _strongest(score, C.JOINT_EDGE, C.SLIVER_MAX, C.SEAM_MIN_END) if lo_at_frame else 0.0
    hi = _strongest(score, 1 - C.SLIVER_MAX, 1 - C.JOINT_EDGE, C.SEAM_MIN_END) if hi_at_frame else 1.0
    return None if lo is None or hi is None else (lo, hi)


def _along(q, rgb):
    """The whole slabs side by side in outline q, or [] when one is cut off by the photo frame. Every end of
    the outline at the frame edge is trimmed at its joint (_ends): left/right on the column profile, top/bottom
    on the row profile. Then one joint splitting the rest into two equal halves is an open book (on an outline
    narrower than two slabs its halves must also mirror); any other joint is a vein."""
    flat, H = _flat(rgb, q)
    h, w = rgb.shape[:2]
    rows = _ends(_joint_profile(np.ascontiguousarray(flat.transpose(1, 0, 2))),
                 min(q[0][1], q[1][1]) < C.FRAME_EDGE * h, max(q[2][1], q[3][1]) > (1 - C.FRAME_EDGE) * h)
    if rows is None:
        return []
    kept = flat[int(rows[0] * flat.shape[0]):int(rows[1] * flat.shape[0])]
    score = _joint_profile(kept)                       # columns: the same shares in kept and flat
    across = _ends(score, min(q[0][0], q[3][0]) < C.FRAME_EDGE * w, max(q[1][0], q[2][0]) > (1 - C.FRAME_EDGE) * w)
    if across is None:
        return []
    lo, hi = across
    span = kept[:, int(lo * kept.shape[1]):int(hi * kept.shape[1])]
    for j in _joints(score, lo + C.JOINT_NMS, hi - C.JOINT_NMS, C.SEAM_MIN):
        s = (j - lo) / (hi - lo)
        if C.SEAM_BAND[0] <= s <= C.SEAM_BAND[1] and \
                (span.shape[1] / span.shape[0] >= C.PAIR_ASPECT or _halves_mirror(span, s)):
            return _cut(q, flat, H, [lo, j, hi], rows)
    return _cut(q, flat, H, [lo, hi], rows)


def _corr(a, b):
    a, b = a - a.mean(), b - b.mean()
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6))


def _joint_scores(left_flat, right_flat):
    """(mirrored, unmirrored) correlation of the coarse veining at the joint. Coarse because two separately
    photographed slabs never register to the pixel; the unmirrored score is the control (a uniform stone
    correlates both ways, a bookmatch only mirrored)."""
    h = C.JOINT_HEIGHT
    def prep(f):
        g = cv2.cvtColor(cv2.resize(f, (int(round(f.shape[1] * h / f.shape[0])), h), interpolation=cv2.INTER_AREA),
                         cv2.COLOR_RGB2GRAY).astype(np.float32)
        d = cv2.GaussianBlur(g, (0, 0), C.JOINT_SIGMA[0] * h) - cv2.GaussianBlur(g, (0, 0), C.JOINT_SIGMA[1] * h)
        return d / (cv2.GaussianBlur(np.abs(d), (0, 0), 0.04 * h) + 2.0)
    L, R = prep(left_flat), prep(right_flat)
    band = int(C.JOINT_BAND * min(L.shape[1], R.shape[1]))
    a, b = L[:, -band:], R[:, :band]
    return _corr(a, b[:, ::-1]), _corr(a, b)


def _joint_mirrors(left_flat, right_flat):
    mirrored, unmirrored = _joint_scores(left_flat, right_flat)
    return mirrored >= C.JOINT_MIRROR_MIN and mirrored - unmirrored >= C.JOINT_MARGIN


def _partner(seg, rgb, q, own, emb):
    """(the bookmatched pair [left, right], the partner's mask) or None: SAM boxed into the slab-sized region past each side edge;
    accepted only when it is a different slab of matching size meeting ours at that edge AND the veining
    mirrors across the joint (a rack neighbour that merely stands beside it is not a partner)."""
    w, h = _size(q)
    tl, tr, br, bl = q
    H, W = rgb.shape[:2]
    top, bottom = float(min(tl[1], tr[1])), float(max(bl[1], br[1]))
    for edge_top, edge_bot, direction in ((tl, bl, -1), (tr, br, 1)):
        edge_x = float((edge_top[0] + edge_bot[0]) / 2)
        x0, x1 = (edge_x - w, edge_x) if direction < 0 else (edge_x, edge_x + w)
        if direction < 0 and edge_x < 0.3 * w or direction > 0 and edge_x > W - 0.3 * w:
            continue                                      # no room for a slab on that side
        box = [max(0.0, x0), max(0.0, top), min(W - 1.0, x1), min(H - 1.0, bottom)]
        m = proto.slab_mask(seg, rgb, box, emb)[0]
        if m.sum() < C.MIN_SLAB_SHARE * 0.5 * m.size or (m & own).sum() / max(1, (m | own).sum()) > C.SAME_IOU:
            continue
        q2 = proto.four_corners(m)
        if q2 is None:
            continue
        w2, h2 = _size(q2)
        mid = (edge_top + edge_bot) / 2
        near = (q2[1] + q2[2]) / 2 if direction < 0 else (q2[0] + q2[3]) / 2
        if np.linalg.norm(near - mid) > C.NEIGHBOUR_GAP * w or \
                not (C.NEIGHBOUR_SIZE[0] <= h2 / h <= C.NEIGHBOUR_SIZE[1] and C.NEIGHBOUR_SIZE[0] <= w2 / w <= C.NEIGHBOUR_SIZE[1]):
            continue
        pair = [q2, q] if direction < 0 else [q, q2]
        if _joint_mirrors(_flat(rgb, pair[0])[0], _flat(rgb, pair[1])[0]):
            return pair, m
    return None


def _in_frame(q, shape):
    """Every corner clear of the frame edge: the whole slab is in the photo (a cut-off slab's shape is unknown)."""
    h, w = shape[:2]
    return bool(((q[:, 0] >= C.FRAME_EDGE * w) & (q[:, 0] <= (1 - C.FRAME_EDGE) * w) &
                 (q[:, 1] >= C.FRAME_EDGE * h) & (q[:, 1] <= (1 - C.FRAME_EDGE) * h)).all())


def load_detector(device):
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
    return AutoModelForZeroShotObjectDetection.from_pretrained(C.DETECTOR).to(device), AutoProcessor.from_pretrained(C.DETECTOR)


def detect(det, rgb, prompt=None, threshold=None):
    """[(x0, y0, x1, y1, score)] for every object the detector finds for `prompt` (default: a stone slab) at
    `threshold` confidence (default C.DETECT_MIN)."""
    import torch
    model, proc = det
    im = Image.fromarray(rgb)
    raw = proc(images=im, text=prompt or C.DETECT_PROMPT, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model(**raw)
    res = proc.post_process_grounded_object_detection(out, raw.input_ids, threshold=threshold or C.DETECT_MIN, text_threshold=0.25,
                                                      target_sizes=[im.size[::-1]])[0]
    return [(*map(float, b), float(s)) for b, s in zip(res["boxes"].cpu(), res["scores"].cpu())]


def _outline_whole(box, quads):
    """The detector's box for this slab does not reach past our outline: SAM found the whole slab, not the part
    between straps or above a rough natural edge. One-directional on purpose: the box may lie INSIDE our outline
    (the detector boxed one slab of an open book)."""
    pts = np.concatenate(quads)
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    w, h = x1 - x0, y1 - y0
    bx0, by0, bx1, by1 = box
    return max((x0 - bx0) / w, (bx1 - x1) / w, (y0 - by0) / h, (by1 - y1) / h) <= C.EXTEND_MAX


def _straps(seg, det, rgb, box, emb):
    """Mask of the straps (leaning planks/bars) in front of the slab in `box`: detected long, narrow boxes
    across it, SAM-masked, kept when the mask is long and thin. Empty when there are none."""
    bx0, by0, bx1, by1 = box
    out = np.zeros(rgb.shape[:2], np.uint8)
    for x0, y0, x1, y1, _ in detect(det, rgb, C.STRAP_PROMPT, C.STRAP_MIN_SCORE):
        if y1 - y0 < C.STRAP_MIN_HEIGHT * (by1 - by0) or x1 - x0 > C.STRAP_MAX_WIDTH * (bx1 - bx0) or x1 < bx0 or x0 > bx1:
            continue
        m = proto.slab_mask(seg, rgb, (x0, y0, x1, y1), emb)[1]
        if not m.any():
            continue
        (_, _), (a, b), _ = cv2.minAreaRect(cv2.findNonZero(m))
        if max(a, b) >= C.STRAP_ELONGATION * max(1.0, min(a, b)):
            out |= m
    k = max(3, int(C.STRAP_PAD * max(rgb.shape[:2])))
    return cv2.dilate(out, np.ones((k, k), np.uint8))


def _without_straps(mask, straps):
    """The slab's outline with the straps taken out: the convex hull of every slab piece beside and between
    them (the straps also stick out above the slab, which would spoil the outline)."""
    pieces = (mask > 0) & (straps == 0)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(pieces.astype(np.uint8))
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= C.STRAP_PIECE_MIN * pieces.sum()]
    if not keep:
        return np.zeros_like(mask)
    pts = cv2.findNonZero(np.isin(labels, keep).astype(np.uint8))
    return cv2.fillConvexPoly(np.zeros_like(mask), cv2.convexHull(pts), 1)


def find_slabs(seg, det, rgb):
    """(quads, mask, raw, straps, reason): quads [] (no whole slab in the photo; reason says why), [quad] (one
    slab) or [left, right] (an open book); mask = the slab(s), holes filled, for compositing; raw = what SAM
    counted as stone (labels are holes; the straps count as shown, never erased: user 2026-10-30); straps = the
    straps in front of it, kept as shot (they complete the outline and its corners only)."""
    h, w = rgb.shape[:2]
    boxes = detect(det, rgb)
    big = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= C.MIN_SLAB_SHARE * w * h]
    none = np.zeros((h, w), np.uint8)
    if not big:
        return [], None, None, none, "no slab detected (close-up or not a slab photo)"
    box = max(big, key=lambda b: b[4])[:4]
    emb = proto.embed(seg, rgb)
    straps = _straps(seg, det, rgb, box, emb)
    if straps.any():                                   # the widest proposal is the whole slab, straps included
        m, raw = proto.slab_mask(seg, rgb, box, emb, widest=True)
        m, raw = _without_straps(m, straps), raw | straps
    else:
        m, raw = proto.slab_mask(seg, rgb, box, emb)
    q = proto.four_corners(m)
    if q is None:
        return [], None, None, straps, "outline is not slab-shaped (several pieces, a rack, a rough shape)"
    quads = _along(q, rgb)
    if not quads:
        return [], None, None, straps, "slab cut off by the photo edge"
    if len(quads) == 1 and (found := _partner(seg, rgb, quads[0], m, emb)) is not None:
        quads, partner = found
        m, raw = m | partner, raw | partner
    if not all(_in_frame(x, rgb.shape) for x in quads):
        return [], None, None, straps, "slab cut off by the photo edge"
    if not _outline_whole(box, quads):
        return [], None, None, straps, "the slab continues past the outline (only part of it found)"
    return quads, m, raw, straps, ""
