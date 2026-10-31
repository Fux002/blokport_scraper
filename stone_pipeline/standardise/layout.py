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
STAND_PATH = HERE / "assets" / "stand_front.jpg"
STAND_FACE = (240, 1284, 853)          # the slab's face in that image: left x, right x, its foot's row
STAND_FLOOR = (600, 700)               # plain floor columns beside the feet, for their shadow
STAND_PAD = (12, 12)                   # px kept round each foot (its shadow)
STAND_FEET = (                         # each foot's outline in that image, traced at 8x
    ((467, 840), (470, 837), (479, 836), (484, 838), (486, 846), (488, 853), (489, 858), (489, 866), (486, 876), (484, 886), (486, 888),
     (485, 893), (450, 894), (449, 889), (455, 884), (457, 880), (457, 858), (462, 852), (464, 846)),
    ((1027, 840), (1031, 836), (1036, 836), (1043, 843), (1047, 850), (1049, 855), (1053, 856), (1054, 888), (1061, 889), (1061, 894),
     (1026, 894), (1026, 889), (1029, 887), (1013, 862), (1014, 859), (1024, 859), (1030, 852), (1027, 846)))


def room_for(key):
    """The room behind a product: chosen from `key` alone, never its vendor or origin, so backgrounds cannot trace a
    vendor (the user's requirement); the same key always gets the same room. An open book stands in the same rooms:
    one hall for the whole catalog."""
    n = int(hashlib.sha256(key.encode()).hexdigest(), 16) % C.ROOM_COUNT + 1
    return _room(f"{n}.jpg").copy()


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
    # A calm, light, neutral concrete, as Blokport's own pages show stone: the room's warm cast taken mostly out.
    lab = cv2.cvtColor(np.clip(rows, 0, 255).astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    lab[..., 0] += C.FLOOR_LIFT
    lab[..., 1:] *= C.FLOOR_CHROMA
    rows = np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1) * 255
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


def two_stones(rgb, alpha):
    """Whether the cut-out holds a second, very different stone along its border: a slab with a broken corner can take
    in the slab standing behind it (a black slab with a white wedge), which would show the wrong stone and the wrong
    shape. Split by lightness (Otsu, so the same photo always splits the same); a vein or breccia pieces spread through
    the stone, the slab behind is one large piece along the border."""
    s = C.TWO_STONES_WORK / max(rgb.shape[:2])
    small = cv2.resize(rgb, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    inside = cv2.resize(alpha.astype(np.float32), small.shape[1::-1], interpolation=cv2.INTER_AREA) > 0.5
    if inside.sum() < 100:
        return False
    lab = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2LAB), (0, 0), C.TWO_STONES_BLUR)
    thr, _ = cv2.threshold(lab[:, :, 0][inside].reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    light = (lab[:, :, 0] > thr) & inside
    dark = inside & ~light
    if not light.any() or not dark.any():
        return False
    gap = float(np.linalg.norm(lab[light].astype(np.float32).mean(0) - lab[dark].astype(np.float32).mean(0)))
    minor = light if light.sum() < dark.sum() else dark
    n, labels, stats, _ = cv2.connectedComponentsWithStats(minor.astype(np.uint8))
    if gap <= C.TWO_STONES_GAP or n < 2:
        return False
    piece = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    ring = inside & (cv2.erode(inside.astype(np.uint8), np.ones((5, 5), np.uint8), borderValue=0) == 0)
    return (stats[piece, cv2.CC_STAT_AREA] >= C.TWO_STONES_PIECE * inside.sum()
            and (ring & (labels == piece)).sum() >= C.TWO_STONES_BORDER * ring.sum())


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
    if any(two_stones(f, a) for f, a in out):
        return "two stones in one outline (the slab behind it, seen past a broken corner?)"
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


@functools.lru_cache(maxsize=1)
def _feet():
    """[(rgb, alpha, shade, centre share, foot-line row, centre x)] of Blokport's two chrome feet, cut once from its front
    studio image (assets/stand_front.jpg, the same stand as its storefront's base image, seen from the front): the
    traced outline is the foot, the floor around it darker than the plain floor beside it is its shadow."""
    im = np.asarray(Image.open(STAND_PATH).convert("RGB")).astype(np.float32)
    feet = []
    for poly in STAND_FEET:
        p = np.array(poly, np.float32)
        x0, y0 = (p.min(0) - STAND_PAD).astype(int)
        x1, y1 = (p.max(0) + STAND_PAD).astype(int)
        rgb = im[y0:y1, x0:x1]
        alpha = np.zeros(rgb.shape[:2], np.float32)
        cv2.fillPoly(alpha, [np.round(p - [x0, y0]).astype(np.int32)], 1.0)
        floor = np.median(im[y0:y1, STAND_FLOOR[0]:STAND_FLOOR[1]], axis=1).mean(axis=1)[:, None]
        shade = np.clip(rgb.mean(axis=2) / np.maximum(floor, 1), 0, 1)
        shade[:STAND_FACE[2] + 2 - y0] = 1.0                            # above the slab's foot it is the slab, not floor
        fade = np.zeros_like(shade)                                      # the shadow fades out before the cut's edges
        fade[STAND_PAD[1] // 2:-STAND_PAD[1] // 2, STAND_PAD[0] // 2:-STAND_PAD[0] // 2] = 1
        shade = 1 - (1 - shade) * cv2.GaussianBlur(fade, (0, 0), STAND_PAD[0] / 4)
        feet.append((rgb, cv2.GaussianBlur(alpha, (0, 0), 0.6), shade,
                     (p[:, 0].mean() - STAND_FACE[0]) / (STAND_FACE[1] - STAND_FACE[0]), STAND_FACE[2] - y0, p[:, 0].mean() - x0))
    return feet


def _stand(out, x, width, bottom):
    """Blokport's chrome stand: its two feet, at the places and the size they have under the slab in its front studio
    image, the slab's foot on their line; each foot's own shadow darkens the floor."""
    s = width / (STAND_FACE[1] - STAND_FACE[0])
    for rgb, alpha, shade, share, line, mid in _feet():
        size = (max(1, round(rgb.shape[1] * s)), max(1, round(rgb.shape[0] * s)))
        r = cv2.resize(rgb, size, interpolation=cv2.INTER_CUBIC)
        a = cv2.resize(alpha, size, interpolation=cv2.INTER_LINEAR)[..., None]
        sh = cv2.resize(shade, size, interpolation=cv2.INTER_LINEAR)[..., None]
        fx, fy = round(x + share * width - mid * s), round(bottom - line * s)
        h = min(size[1], out.shape[0] - fy)
        region = out[fy:fy + h, fx:fx + size[0]]
        region[:] = region * sh[:h] * (1 - a[:h]) + r[:h] * a[:h]


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
    out = room_for(key) if room is None else room.copy()
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
            _stand(out, x, sw, bottom)
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
    stand = (np.abs(with_it - without).max(axis=2) > C.STAND_MIN_CHANGE).astype(np.uint8)
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


def on_white(slabs):
    """The slab(s) on plain white with a soft shadow, no room and no stand (no room to clash with, nothing that can trace a
    vendor)."""
    return _on_white(*_white_slabs(slabs))
