"""Standard look: every cleaned, squared slab (or open-book pair) in ONE showroom, each slab on the SAME stand.
Every image is the same canvas: the slab(s) as large as fit, on the same floor line."""
from stone_pipeline.config.settings import STANDARDISE as C
import functools
import hashlib
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from stone_pipeline.standardise import outline as openbook
from stone_pipeline.standardise import marks as proto

HERE = Path(__file__).parent
ROOM_DIR = HERE / "assets" / "rooms"   # the user's showroom, fixed assets (made once from their example photo)
STEEL = np.array((1.0, 1.0, 1.03), np.float32)    # black steel's tint


def room_for(key, wide):
    """The room behind a product: chosen from `key` alone, never its vendor or origin, so backgrounds cannot trace a
    vendor (the user's requirement); the same key always gets the same room."""
    n = int(hashlib.sha256(key.encode()).hexdigest(), 16) % C.ROOM_COUNT + 1
    return _room(f"{'wide_' if wide else ''}{n}.jpg").copy()


@functools.lru_cache(maxsize=None)
def _room(name):
    return clean_floor(np.asarray(Image.open(ROOM_DIR / name).convert("RGB")).astype(np.float32))


def clean_floor(room):
    """The room with its near floor clean: the example photo's glossy floor mirrors lamps and racks where our slab now
    stands, and keeps a band from the stand that was in it. From FLOOR_CLEAN[0] down it fades into each row's median
    colour (smoothed down the rows, so no band stays) with the photo's own fine grain, fully by FLOOR_CLEAN[1]; the slab
    adds its own reflection on top (_reflect)."""
    h = room.shape[0]
    start, full = int(C.FLOOR_CLEAN[0] * h), int(C.FLOOR_CLEAN[1] * h)
    out = room.copy()
    floor = room[start:]
    rows = cv2.GaussianBlur(np.median(floor, axis=1)[:, None, :], (0, 0), sigmaX=0.1, sigmaY=C.FLOOR_CLEAN_ROWS * h)
    clean = rows + C.FLOOR_GRAIN * (floor - cv2.GaussianBlur(floor, (0, 0), 1.5))
    w = np.clip((np.arange(start, h) - start) / max(1, full - start), 0, 1)[:, None, None]
    out[start:] = floor * (1 - w) + clean * w
    return out


def fit_working(rgb):
    """rgb at no more than the working size: the models' cost and results do not depend on a huge original."""
    s = C.WORK_LONG_EDGE / max(rgb.shape[:2])
    return cv2.resize(rgb, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else rgb


def upscale(rgb, esr, factor):
    """rgb enlarged by `factor` (> 1) with Real-ESRGAN's fine detail but the photo's OWN tone and colour: the
    smooth upscale gives every tone and colour (ESRGAN alone crushes near-black, washes faint colour, tints
    greys); ESRGAN adds only the detail finer than C.DETAIL_SCALE source pixels, on L* only."""
    size = (int(round(rgb.shape[1] * factor)), int(round(rgb.shape[0] * factor)))
    base = cv2.cvtColor(cv2.resize(rgb, size, interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    sharp = cv2.resize(cv2.cvtColor(esr.enhance(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)), cv2.COLOR_BGR2RGB), size,
                       interpolation=cv2.INTER_AREA)
    sharp_l = cv2.cvtColor(sharp.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)[:, :, 0]
    sigma = max(1.0, factor * C.DETAIL_SCALE)
    base[:, :, 0] = cv2.GaussianBlur(base[:, :, 0], (0, 0), sigma) + sharp_l - cv2.GaussianBlur(sharp_l, (0, 0), sigma)
    return np.clip(cv2.cvtColor(base, cv2.COLOR_LAB2RGB) * 255 + 0.5, 0, 255).astype(np.uint8)


def outline_with_erased(outline, marks, inside):
    """The slab's outline including what was erased on it: a card or sticker on the edge is not a bite out of the
    stone, its place is filled with stone. Only within the fitted slab, so a mark on the floor never grows it."""
    return outline | (marks & inside)


def squared_slabs(rgb, seg, det, lama, esr):
    """One/two (squared slab RGB, its alpha) pairs, cleaned and lit as one set (a pair keeps one light), or the
    reason (str) no whole slab was found.
    The alpha is the slab's own outline: a chipped corner shows the plate, never the warehouse behind it."""
    quads, mask, raw, straps, reason = openbook.find_slabs(seg, det, rgb)
    if not quads:
        return reason
    # The slab's own outline, not a ruled rectangle (the user: squared-off edges look fake): what SAM saw as this
    # stone with its holes filled, inside the fitted corners. Closing keeps a real chip or rough edge but fills a
    # thin notch from a label or hook sitting on the edge (its stone is erased and refilled below). The straps
    # stay as shot (the user's choice), part of the slab's shape: their clamps often sit on its corners.
    inside = np.zeros_like(mask)
    for q in quads:
        cv2.fillConvexPoly(inside, q.astype(np.int32), 1)
    body = mask | (straps & inside)
    hull = cv2.fillConvexPoly(np.zeros_like(mask), cv2.convexHull(cv2.findNonZero(body)), 1)
    k = max(3, int(C.OUTLINE_CLOSE * max(rgb.shape[:2])))
    seen = cv2.morphologyEx((raw | straps) & hull, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    contours, _ = cv2.findContours(seen, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    outline = cv2.drawContours(np.zeros_like(mask), contours, -1, 1, -1)
    # One erase of everything foreign: text is read on the photo as shot (a half-erased label no longer reads
    # as text), plus the labels/clips SAM did not count as stone inside each slab.
    marks = proto.mark_mask(rgb, hull) | proto.flare_mask(rgb, hull & (straps == 0))
    for q in quads:
        marks |= proto.foreign_objects(q, raw)
    rgb = proto.erase(rgb, marks, lama, hull)
    outline = outline_with_erased(outline, marks, inside)
    flats, alphas = [], []
    for q in quads:
        f, H = openbook._flat(rgb, q)
        flats.append(f)
        alphas.append(cv2.warpPerspective(outline.astype(np.float32), H, f.shape[1::-1], flags=cv2.INTER_LINEAR))
    h = min(f.shape[0] for f in flats)
    size = [(int(round(f.shape[1] * h / f.shape[0])), h) for f in flats]
    flats = [cv2.resize(f, s, interpolation=cv2.INTER_AREA) for f, s in zip(flats, size)]
    alphas = [cv2.resize(a, s, interpolation=cv2.INTER_AREA) for a, s in zip(alphas, size)]
    factor = (C.OUT_W * (1 - 2 * C.MARGIN)) / sum(s[0] for s in size)    # how much the layout will enlarge them
    if factor > 1:
        flats = [upscale(f, esr, factor) for f in flats]
        alphas = [cv2.resize(a, f.shape[1::-1], interpolation=cv2.INTER_LINEAR) for a, f in zip(alphas, flats)]
    joined = proto.even_light(np.concatenate(flats, axis=1), (np.concatenate(alphas, axis=1) > 0.5).astype(np.uint8))
    out, x = [], 0
    for f, a in zip(flats, alphas):
        out.append((joined[:, x:x + f.shape[1]], a))
        x += f.shape[1]
    if len(out) == 2:                                   # the polished edge faces at the seam catch the light
        (l, la), (r, ra) = out
        kl, kr = max(1, int(C.JOINT_TRIM * l.shape[1])), max(1, int(C.JOINT_TRIM * r.shape[1]))
        out = [(l[:, :-kl], la[:, :-kl]), (r[:, kr:], ra[:, kr:])]
    return out


def _cover(img, w, h):
    """img scaled to cover a w x h frame, bottom-anchored (the floor stays at the bottom), centred across."""
    s = max(w / img.shape[1], h / img.shape[0])
    big = cv2.resize(img, (int(np.ceil(img.shape[1] * s)), int(np.ceil(img.shape[0] * s))), interpolation=cv2.INTER_LINEAR)
    x = (big.shape[1] - w) // 2
    return big[big.shape[0] - h:, x:x + w].copy()


def _trim_side(lab, alpha, band):
    """alpha with what is not this stone cut from its left side (rotated to reach the others): per row, walking out
    from just inside the edge, the first pixel unlike the stone beside it starts what is cut. Along the rows the cut
    is a median, so a vein reaching the edge is no strip and stays."""
    h, w = alpha.shape
    n = max(4, int(C.TRIM_REACH * w))
    near = cv2.blur(lab[:, n:n + band], (band, max(3, int(C.TRIM_SMOOTH * h)))).mean(axis=1, keepdims=True)
    def apart(block):                                  # colour difference from the stone beside it, brightness damped
        off = block - near
        return np.hypot(np.hypot(off[:, :, 1], off[:, :, 2]), C.TRIM_L_WEIGHT * off[:, :, 0])
    # A busy stone varies on its own: what counts as unlike is measured on this stone, just inside the reach.
    limit = max(C.TRIM_UNLIKE, C.TRIM_OWN * float(np.percentile(apart(lab[:, n:n + 2 * band]), C.TRIM_OWN_PCT)))
    unlike = apart(lab[:, :n]) > limit
    # Walking outward: the deepest column of each row where the stone stops matching (0: nothing to cut).
    cut = ((n - np.argmax(unlike[:, ::-1], axis=1)) * unlike.any(axis=1)).astype(np.float32)
    k = max(3, int(C.TRIM_RUN * h))
    # The slab's physical edge: where a neighbour's side stands against the stone, a line of sudden change running
    # along the edge, stronger than anything in this stone's own veining further in.
    def line(block):
        step = np.diff(block, axis=1)
        strength = np.hypot(np.hypot(step[:, :, 1], step[:, :, 2]), C.TRIM_L_WEIGHT * step[:, :, 0])
        return cv2.blur(strength, (1, k))
    edge, inner = line(lab[:, :n + 1]), line(lab[:, n:n + 2 * band])
    at = edge.argmax(axis=1)
    found = edge[np.arange(h), at] > C.TRIM_LINE * float(np.percentile(inner, C.TRIM_LINE_PCT))
    cut = np.maximum(cut, (at + 1) * found)
    cut = np.array([np.median(cut[max(0, i - k // 2):i + k // 2 + 1]) for i in range(h)], np.float32)
    # A neighbour's side runs a long way along an edge; a short cut is a vein meeting the edge, and would be a nick.
    n, labels, stats, _ = cv2.connectedComponentsWithStats((cut > 0).astype(np.uint8).reshape(-1, 1))
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_HEIGHT] < C.TRIM_MIN_LENGTH * h:
            cut[labels.ravel() == i] = 0
    # A real edge has no steps.
    cut = cv2.GaussianBlur(cut.reshape(-1, 1), (0, 0), sigmaX=1, sigmaY=C.TRIM_SOFT * h).ravel()
    out = alpha.copy()
    for y, c in enumerate(cut.astype(int)):
        out[y, :c] = 0
    return out


def _fill_bites(alpha):
    """A label's square bite in an edge is no chip: bites narrower than NOTCH_FILL are filled. Padded, so the outside
    counts as background and a thin strip along a whole side (what trim_edges then cuts) is no bite."""
    k = max(3, int(C.NOTCH_FILL * alpha.shape[0]))
    padded = cv2.copyMakeBorder(alpha, k, k, k, k, cv2.BORDER_CONSTANT, value=0)
    return cv2.morphologyEx(padded, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))[k:-k, k:-k]


def trim_edges(rgb, alpha, skip=()):
    """The slab's outline tightened on the stone itself: a strip of a neighbour, a rack or a shadow left inside the
    fitted outline is cut, so the room shows there instead. `skip`: sides that are an open book's seam."""
    lab = cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    band = max(3, int(C.TRIM_BAND * rgb.shape[1]))
    out = alpha
    for side, turns in (("left", 0), ("top", 3), ("right", 2), ("bottom", 1)):
        if side in skip:
            continue
        out = np.rot90(_trim_side(np.rot90(lab, turns), np.rot90(out, turns), band), -turns)
    return np.ascontiguousarray(out)


def _stand(out, x, width, bottom, scale):
    """A black steel stand, drawn so it is sharp: two feet joined by a rail, the slab standing IN the rail behind its
    front lip. `scale`: an open book's slabs and room are drawn smaller (seen from further away), so is its stand. It
    spans the slab inset at both ends; the floor is under the feet."""
    x0, x1 = x + int(C.STAND_INSET * width), x + width - int(C.STAND_INSET * width)
    foot_w, above = int(C.FOOT_WIDTH * scale), int(C.FOOT_ABOVE * scale)
    top, rail_end, floor = bottom - int(C.RAIL_LIP * scale), bottom + int((C.RAIL_HEIGHT - C.RAIL_LIP) * scale), bottom + int(C.FOOT_BELOW * scale)
    # What the stand throws on the floor: a soft shade under the rail, a dark contact line under each foot.
    shade = np.zeros(out.shape[:2], np.float32)
    shade[rail_end:floor, x0 + foot_w:x1 - foot_w] = C.RAIL_SHADE
    out *= (1 - cv2.GaussianBlur(shade, (0, 0), 12))[:, :, None]
    foot = np.zeros(out.shape[:2], np.float32)
    for fx in (x0, x1 - foot_w):
        foot[floor - 3:floor + 4, fx - 2:fx + foot_w + 2] = 1
    out *= (1 - C.FOOT_SHADOW * cv2.GaussianBlur(foot, (0, 0), 5))[:, :, None]
    # The steel, on its own layer so its edges can be softened as a lens would.
    layer, alpha = np.zeros(out.shape, np.float32), np.zeros(out.shape[:2], np.float32)

    def face(y0, y1, fx0, fx1, shades):
        ramp = np.interp(np.arange(y1 - y0), [0, max(1, y1 - y0 - 1)], shades).astype(np.float32)
        layer[y0:y1, fx0:fx1] = ramp[:, None, None] * STEEL
        alpha[y0:y1, fx0:fx1] = 1
    face(top, rail_end, x0, x1, C.RAIL_SHADES)                                     # the rail's front face
    face(top, top + 2, x0, x1, (C.RAIL_LIGHT, C.RAIL_LIGHT))                         # the light along its upper edge
    for fx in (x0, x1 - foot_w):
        face(bottom - above, floor, fx, fx + foot_w, C.FOOT_SHADES)       # a foot's front face
        face(bottom - above, bottom - above + 3, fx, fx + foot_w, (C.RAIL_LIGHT, C.RAIL_LIGHT))
    keep = cv2.GaussianBlur(alpha, (0, 0), 0.7)[:, :, None]
    # Divided by the softened mask, or the softened edges would be dark halos of the empty layer.
    steel = cv2.GaussianBlur(layer, (0, 0), 0.7) / np.maximum(keep, 1e-3)
    out[:] = out * (1 - keep) + steel * keep


def _reflect(out, left, total, bottom):
    """The slab mirrored in the matte concrete at its foot: faint, fading and blurring with the distance."""
    rows = C.OUT_H - bottom
    mirrored = out[bottom - rows:bottom, left:left + total][::-1]
    near = cv2.GaussianBlur(mirrored, (0, 0), C.REFLECT_BLUR[0])
    far = cv2.GaussianBlur(mirrored, (0, 0), C.REFLECT_BLUR[1])
    t = np.arange(rows, dtype=np.float32)[:, None, None]
    spread = np.clip(t / (2 * C.REFLECT_FALL * C.OUT_H), 0, 1)
    strength = C.REFLECT * np.exp(-t / (C.REFLECT_FALL * C.OUT_H))
    # Soft at the sides: a reflection in a real floor has no hard vertical edge.
    edge = np.clip(np.minimum(np.arange(total), np.arange(total)[::-1]) / (C.REFLECT_SIDE * C.OUT_W), 0, 1)[None, :, None].astype(np.float32)
    floor = out[bottom:, left:left + total]
    floor[:] = floor * (1 - strength * edge) + (near * (1 - spread) + far * spread) * strength * edge


def _ground(out, left, total, top, bottom):
    """What ties the slab to the floor: the stone's foot in less light, its bottom edge face, the dark floor under
    it, and the soft shadow spilling past its ends."""
    edge = max(2, int(C.EDGE_HEIGHT * C.OUT_H))
    rows = np.arange(top, bottom, dtype=np.float32)
    fade = 1 - C.FOOT_FADE[0] * np.exp(-(bottom - rows) / (C.FOOT_FADE[1] * C.OUT_H))
    out[top:bottom, left:left + total] *= fade[:, None, None]
    out[bottom - edge:bottom, left:left + total] *= C.EDGE
    depth = np.arange(C.OUT_H, dtype=np.float32)[:, None]
    fall = np.exp(-np.maximum(depth - bottom, 0) / (C.CONTACT_DEPTH * C.OUT_H))
    under = np.zeros(out.shape[:2], np.float32)
    under[bottom:, left:left + total] = 1
    reach = C.CAST_REACH * C.OUT_W
    spill = cv2.GaussianBlur(under, (0, 0), sigmaX=reach / 2, sigmaY=3) * C.CAST
    shade = np.maximum(cv2.GaussianBlur(under, (0, 0), sigmaX=reach / 4, sigmaY=3) * C.CONTACT, spill) * fall
    shade[:bottom] = 0
    out *= (1 - shade)[:, :, None]


def compose(slabs, key, room=None, stand=True):
    """The slab(s) as large as fits the one canvas, centred, standing on the one floor line, each on its own
    stand: every image the same size and shape, so the storefront's 3:2 frames show it whole with no bars or
    crop. A wide slab or open book shows more room above it, never a smaller or moved floor."""
    m = int(C.MARGIN * C.OUT_W)
    bottom = C.OUT_H - int(round(C.FLOOR_SHARE * C.OUT_H))
    joint = C.JOINT if len(slabs) > 1 else 0
    gaps = joint * (len(slabs) - 1)
    width = sum(s.shape[1] for s, _ in slabs)
    scale = min((C.OUT_W - 2 * m - gaps) / width, (bottom - m) / slabs[0][0].shape[0])
    sh = int(round(slabs[0][0].shape[0] * scale))
    total = int(round(width * scale)) + gaps
    left = (C.OUT_W - total) // 2
    top = bottom - sh
    out = room_for(key, len(slabs) > 1) if room is None else room.copy()
    # The floor's shade right under the slab, fading towards the viewer (a soft light from above).
    fall = np.exp(-np.maximum(np.arange(C.OUT_H, dtype=np.float32) - bottom, 0) / (C.AMBIENT_FALL * C.OUT_H))[:, None]
    shadow = np.zeros(out.shape[:2], np.float32)
    shadow[bottom:, left:left + total] = 1
    out *= (1 - C.SHADOW * cv2.GaussianBlur(shadow, (0, 0), 6) * fall)[:, :, None]
    x, placed = left, []
    for i, (s, a) in enumerate(slabs):
        # An open book's seam is no outer edge.
        a = trim_edges(s, _fill_bites(a), skip=("right",) if i == 0 and len(slabs) > 1 else ("left",) if i and len(slabs) > 1 else ())
        sw = int(round(s.shape[1] * scale)) if i < len(slabs) - 1 else left + total - x
        rgb = cv2.resize(s, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
        # A real edge is crisp: the outline taken a little inside the photo's mixed edge pixels, barely softened.
        solid = cv2.erode((cv2.resize(a, (sw, sh), interpolation=cv2.INTER_AREA) > 0.5).astype(np.uint8), np.ones((C.EDGE_INSET * 2 + 1,) * 2, np.uint8))
        alpha = cv2.GaussianBlur(solid.astype(np.float32), (0, 0), C.EDGE_SOFT)[:, :, None]
        out[top:bottom, x:x + sw] = alpha * rgb + (1 - alpha) * out[top:bottom, x:x + sw]
        placed.append((x, sw))
        x += sw
        if i < len(slabs) - 1:
            out[top:bottom, x:x + joint] = C.JOINT_COLOUR
            x += joint
    _reflect(out, left, total, bottom)
    _ground(out, left, total, top, bottom)
    for x, sw in placed:
        if stand:
            _stand(out, x, sw, bottom, 1 / len(slabs))
    return np.clip(out, 0, 255).astype(np.uint8)


def masks(slabs, key):
    """(where slab or stand is opaque, where the stand's steel is) on the canvas: what does not change with the room
    behind it, and what changes when the stand is left out."""
    grey = np.full((C.OUT_H, C.OUT_W, 3), 128, np.float32)
    dark = compose(slabs, key, room=np.zeros_like(grey)).astype(np.int16)
    light = compose(slabs, key, room=np.full_like(grey, 255)).astype(np.int16)
    with_it = compose(slabs, key, room=grey).astype(np.int16)
    without = compose(slabs, key, room=grey, stand=False).astype(np.int16)
    solid = (np.abs(dark - light).max(axis=2) < C.OPAQUE_MAX_CHANGE).astype(np.uint8)
    stand = ((np.abs(with_it - without).max(axis=2) > C.STAND_MIN_CHANGE) & (with_it.max(axis=2) < C.STEEL_MAX)).astype(np.uint8)
    return solid, stand


def _white_slabs(slabs):
    """(colour, coverage) of the slab(s) laid out on the white canvas, centred, as large as the border allows."""
    fixed = []
    for i, (s, a) in enumerate(slabs):
        skip = ("right",) if i == 0 and len(slabs) > 1 else ("left",) if i and len(slabs) > 1 else ()
        fixed.append((s, trim_edges(s, _fill_bites(a), skip=skip)))
    gap = C.WHITE_SEAM if len(fixed) > 1 else 0
    tw, th = sum(s.shape[1] for s, _ in fixed), fixed[0][0].shape[0]
    k = min((C.OUT_W * (1 - 2 * C.WHITE_MARGIN) - gap) / tw, C.OUT_H * (1 - 2 * C.WHITE_MARGIN) / th)
    sh, total = int(round(th * k)), int(round(tw * k)) + gap
    x, y = (C.OUT_W - total) // 2, (C.OUT_H - sh) // 2
    rgb = np.full((C.OUT_H, C.OUT_W, 3), 255, np.float32)
    cover = np.zeros((C.OUT_H, C.OUT_W), np.float32)
    for s, a in fixed:
        sw = int(round(s.shape[1] * k))
        solid = cv2.erode((cv2.resize(a, (sw, sh), interpolation=cv2.INTER_AREA) > 0.5).astype(np.uint8), np.ones((C.EDGE_INSET * 2 + 1,) * 2, np.uint8))
        cover[y:y + sh, x:x + sw] = cv2.GaussianBlur(solid.astype(np.float32), (0, 0), C.EDGE_SOFT)
        rgb[y:y + sh, x:x + sw] = cv2.resize(s, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
        x += sw + gap
    return rgb, cover


def _on_white(rgb, cover):
    out = np.full((C.OUT_H, C.OUT_W, 3), 255, np.float32)
    tint = np.array(C.WHITE_TINT, np.float32)
    for blur, drop, strength in C.WHITE_SHADOWS:
        s = np.roll(cv2.GaussianBlur(cover, (0, 0), blur), drop, axis=0)[..., None] * strength
        out = out * (1 - s) + tint * s
    a = cover[..., None]
    return (out * (1 - a) + rgb * a).clip(0, 255).astype(np.uint8)


def on_white(slabs, turn=None):
    """The slab(s) on plain white with a soft shadow, no room and no stand (no room to clash with, nothing that can trace a
    vendor). turn = "left" | "right": the same slab turned, about its middle."""
    rgb, cover = _white_slabs(slabs)
    if turn:
        w, h = C.OUT_W, C.OUT_H
        inset, cy = C.ANGLE_NARROW * w, h / 2
        l, r = (C.ANGLE_FAR, C.ANGLE_NEAR) if turn == "left" else (C.ANGLE_NEAR, C.ANGLE_FAR)
        x0, x1 = (inset, w) if turn == "left" else (0, w - inset)
        dst = np.array([[x0, cy - cy * l], [x1, cy - cy * r], [x1, cy + cy * r], [x0, cy + cy * l]], np.float32)
        H = cv2.getPerspectiveTransform(np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32), dst)
        rgb = cv2.warpPerspective(rgb, H, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
        cover = cv2.warpPerspective(cover, H, (w, h), flags=cv2.INTER_LINEAR)
    return _on_white(rgb, cover)
