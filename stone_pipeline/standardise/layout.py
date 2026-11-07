"""Standard look: every cleaned, squared slab (or open-book pair) in ONE showroom, each slab on the SAME stand.
Every image is the same canvas: the slab(s) as large as fit, on the same floor line."""
from stone_pipeline.config.settings import STANDARDISE as C
import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from stone_pipeline.standardise import outline as openbook
from stone_pipeline.standardise import marks as proto
from stone_pipeline.standardise import studio

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


def room_for(key, far):
    """The room behind a product: chosen from `key` alone, never its vendor or origin, so backgrounds cannot trace a
    vendor (the user's requirement); the same key always gets the same room. far: the same hall seen from twice as far,
    for an open book, which is drawn at half a slab's size (assets/rooms/far_<n>.jpg: the real hall at half size in the
    middle, the band around it generated once to continue it)."""
    n = int(hashlib.sha256(key.encode()).hexdigest(), 16) % C.ROOM_COUNT + 1
    return _room(f"{'far_' if far else ''}{n}.jpg").copy()


@functools.lru_cache(maxsize=None)
def _room(name):
    room = np.asarray(Image.open(ROOM_DIR / name).convert("RGB")).astype(np.float32)
    if name.startswith("far_"):                        # out of focus behind slabs in focus, like the regular halls
        room = cv2.GaussianBlur(room, (0, 0), C.FAR_ROOM_BLUR)
    return room


@dataclass(frozen=True)
class Flat:
    """One squared slab at the work copy's pixels, and what it was squared from: a close-up takes the same slab from the
    photo at its full resolution (views.close_up)."""
    rgb: np.ndarray
    alpha: np.ndarray
    homography: np.ndarray  # a pixel of the work copy -> the same point on this squared slab
    work: np.ndarray        # the work copy it was squared from, its logo and marks already taken off


def scaling(sx, sy):
    """The homography of an image resized by (sx, sy), pixel centres mapped as cv2.resize maps them."""
    return np.array([[sx, 0, (sx - 1) / 2], [0, sy, (sy - 1) / 2], [0, 0, 1]], np.float64)


def fit_working(rgb):
    """rgb at no more than the working size: the models' cost and results do not depend on a huge original."""
    s = C.WORK_LONG_EDGE / max(rgb.shape[:2])
    return cv2.resize(rgb, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else rgb


def upscale(rgb, esr, factor):
    """rgb enlarged by `factor` (> 1) with Real-ESRGAN's fine detail but the photo's OWN tone and colour: the
    smooth upscale gives every tone and colour (ESRGAN alone crushes near-black, washes faint colour, tints
    greys); ESRGAN adds only the detail finer than C.DETAIL_SCALE source pixels, on L* only."""
    size = _drawn_size(rgb, factor)
    base = cv2.cvtColor(cv2.resize(rgb, size, interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    sharp = cv2.resize(cv2.cvtColor(esr.enhance(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)), cv2.COLOR_BGR2RGB), size,
                       interpolation=cv2.INTER_AREA)
    sharp_l = cv2.cvtColor(sharp.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)[:, :, 0]
    sigma = max(1.0, factor * C.DETAIL_SCALE)
    base[:, :, 0] = cv2.GaussianBlur(base[:, :, 0], (0, 0), sigma) + sharp_l - cv2.GaussianBlur(sharp_l, (0, 0), sigma)
    return np.clip(cv2.cvtColor(base, cv2.COLOR_LAB2RGB) * 255 + 0.5, 0, 255).astype(np.uint8)


def outline_with_erased(outline, cards, objects, inside, stone):
    """The slab's outline including what was erased on it, inside the fitted slab (a mark on the floor never grows it):
    a printed card or label (cards: the text marks) sits on the stone and is filled whole, also on a corner; an object
    SAM did not count as stone (objects: foreign_objects) only within the stone's own hull (stone, one per slab): on an
    edge it has stone on both sides of it, but where the stone itself stops short of the fitted corners (a rounded,
    chipped or broken corner, an open book's inner corner) what the photo shows there is the wall behind, never stone;
    it stays out, so the corner keeps its true shape."""
    return outline | ((cards | (objects & stone)) & inside)


def wall_in_corners(cards, objects, inside, stone):
    """What the rule before OUTLINE_CHECKED counted as stone and this one leaves out: objects inside the fitted slab,
    outside the stone's own hull, not under a card (the wall in a rounded or broken corner)."""
    return objects & inside & (stone == 0) & (cards == 0)


def stone_hulls(body, quads):
    """The stone's own hull, one per slab: the convex hull of what SAM saw as stone inside each fitted slab, joined. One
    hull over an open book would bridge the corners where its halves meet, and take the wall there in."""
    out = np.zeros_like(body)
    for q in quads:
        part = body & cv2.fillConvexPoly(np.zeros_like(body), q.astype(np.int32), 1)
        if part.any():
            cv2.fillConvexPoly(out, cv2.convexHull(cv2.findNonZero(part)), 1)
    return out


def side_reached(found):
    """Whether the studio draws its sawn edge on every row of the slab(s): beside a broken right corner it does not (the
    rule before OUTLINE_CHECKED drew one there from the wall). Decided exactly as studio.place draws it (studio.sawn_edge)
    on the outlines as enlarged draws them (drawn_outlines of flat_outlines), from the outline step alone."""
    return studio.sawn_edge(drawn_outlines(flat_outlines(found))).min() == 1


def hollow(alpha):
    """The share of the cut-out's convex hull it does not cover: a stand or rack in front of the slab bites a hollow
    out of its outline (the stone behind it is not seen, so it is cut away); a chip or a rough natural edge barely
    does (it is outside, not a bite)."""
    inside = (alpha > 0.5).astype(np.uint8)
    if not inside.any():
        return 1.0
    hull = cv2.fillConvexPoly(np.zeros_like(inside), cv2.convexHull(cv2.findNonZero(inside)), 1)
    return float(((hull > 0) & (inside == 0)).sum() / hull.sum())


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


@dataclass(frozen=True)
class Outline:
    """The outline step of a photo (slab_outline): the fitted slabs and what is stone in them, before anything is drawn."""
    quads: list        # the fitted slabs (one, or an open book's two)
    aspects: list      # each one's width/height to square it at (steep_aspect), None: its sides measure it
    inside: np.ndarray   # the fitted slabs' area
    hull: np.ndarray     # the convex hull of what SAM saw as stone (and the straps on it): where marks are read
    stone: np.ndarray    # the stone's own hull, one per slab (stone_hulls): what an erased object may grow into
    seen: np.ndarray     # what SAM saw as stone, closed over thin notches, holes filled
    cards: np.ndarray    # the printed cards and labels (text marks)
    objects: np.ndarray  # what SAM did not count as stone inside each slab (foreign_objects)

    def outline(self):
        """The slab's outline, what was erased on it included (outline_with_erased)."""
        return outline_with_erased(self.seen, self.cards, self.objects, self.inside, self.stone)


def slab_outline(rgb, seg, det, size=None):
    """The outline step (Outline) of the work copy rgb, or the reason (str) no whole slab was found. size = (length,
    width): a slab shot at a steep angle is squared to it."""
    quads, mask, raw, straps, reason = openbook.find_slabs(seg, det, rgb)
    if not quads:
        return reason
    aspects = [openbook.steep_aspect(q, size) for q in quads]
    if refused := next((a for a in aspects if isinstance(a, str)), None):
        return refused
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
    closed = cv2.morphologyEx((raw | straps) & hull, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    seen = cv2.drawContours(np.zeros_like(mask), contours, -1, 1, -1)
    # One erase of everything foreign: text is read on the photo as shot (a half-erased label no longer reads
    # as text), plus the labels/clips SAM did not count as stone inside each slab.
    cards = proto.mark_mask(rgb, hull)                 # lamp reflections are the retouch model's (retouch.reflections)
    objects = np.zeros_like(mask)
    for q in quads:
        objects |= proto.foreign_objects(q, raw)
    return Outline(quads, aspects, inside, hull, stone_hulls(body, quads), seen, cards, objects)


def drawn_differently(found):
    """Whether this version draws the slab differently from the rule before OUTLINE_CHECKED: the wall in a corner left
    out, or the studio's sawn edge stopping beside a broken right corner."""
    return bool(wall_in_corners(found.cards, found.objects, found.inside, found.stone).any()) or not side_reached(found)


def flat_slabs(rgb, seg, det, lama, size=None):
    """One/two Flat slabs squared from the work copy rgb (a pair at one height), cleaned, or the reason (str) no whole
    slab was found. size = (length, width): a slab shot at a steep angle is squared to it. The alpha is the slab's own
    outline: a chipped corner shows the plate, never the warehouse behind it."""
    found = slab_outline(rgb, seg, det, size)
    return found if isinstance(found, str) else flats_from(rgb, found, lama)


def _one_height(sizes):
    """(width, height) of each squared slab of `sizes` brought to the lowest one's height (a pair side by side)."""
    h = min(s[1] for s in sizes)
    return [(int(round(w * h / sh)), h) for w, sh in sizes]


def flat_outlines(found):
    """Each slab's outline (Outline.outline) squared and at one height, exactly as flats_from draws it, from the outline
    step alone: no eraser."""
    outline = found.outline().astype(np.float32)
    squares = [openbook.square(q, aspect) for q, aspect in zip(found.quads, found.aspects)]
    return [cv2.resize(cv2.warpPerspective(outline, H, size, flags=cv2.INTER_LINEAR), s, interpolation=cv2.INTER_AREA)
            for (H, size), s in zip(squares, _one_height([size for _, size in squares]))]


def flats_from(rgb, found, lama):
    """flat_slabs from its outline step (slab_outline): the marks erased, each slab squared, a pair at one height, its
    outline flat_outlines'."""
    rgb = proto.erase(rgb, found.cards | found.objects, lama, found.hull)
    squared = [openbook._flat(rgb, q, aspect) for q, aspect in zip(found.quads, found.aspects)]
    size = _one_height([f.shape[1::-1] for f, _ in squared])
    squares = [scaling(s[0] / f.shape[1], s[1] / f.shape[0]) @ H for (f, H), s in zip(squared, size)]
    flats = [cv2.resize(f, s, interpolation=cv2.INTER_AREA) for (f, _), s in zip(squared, size)]
    return [Flat(f, a, H, rgb) for f, a, H in zip(flats, flat_outlines(found), squares)]


def _drawn_size(img, factor):
    """(width, height) of img enlarged `factor` times."""
    return int(round(img.shape[1] * factor)), int(round(img.shape[0] * factor))


def _drawn_factor(alphas):
    """How much the layout enlarges the flat slabs (flat_slabs, by their outlines: the same size): above 1 they are
    enlarged, else drawn as they are."""
    return (C.OUT_W * (1 - 2 * C.MARGIN)) / sum(a.shape[1] for a in alphas)


def _drawn_alphas(alphas, factor):
    if factor <= 1:
        return list(alphas)
    return [cv2.resize(a, _drawn_size(a, factor), interpolation=cv2.INTER_LINEAR) for a in alphas]


def _trim_seam(images):
    """An open book's two halves with each one's edge face at the seam cut off (JOINT_TRIM of its width: the polished
    faces catch the light); a single slab as it is."""
    if len(images) != 2:
        return list(images)
    l, r = images
    kl, kr = max(1, int(C.JOINT_TRIM * l.shape[1])), max(1, int(C.JOINT_TRIM * r.shape[1]))
    return [l[:, :-kl], r[:, kr:]]


def drawn_outlines(alphas):
    """Flat slabs' outlines (Flat.alpha, flat_outlines) exactly as enlarged draws them (enlarged, an open book's seam
    trimmed): no upscaler."""
    return _trim_seam(_drawn_alphas(alphas, _drawn_factor(alphas)))


def drawn_alphas(slabs):
    """The flat slabs' outlines exactly as enlarged draws them (drawn_outlines), made from the flat slabs alone."""
    return drawn_outlines([f.alpha for f in slabs])


def enlarged(slabs, esr):
    """The flat slabs (flat_slabs) at the size the layout draws them, lit as one set (a pair keeps one light), or the
    reason (str) they cannot be shown."""
    factor = _drawn_factor([f.alpha for f in slabs])
    flats = [upscale(f.rgb, esr, factor) if factor > 1 else f.rgb for f in slabs]
    alphas = _drawn_alphas([f.alpha for f in slabs], factor)
    joined = proto.even_light(np.concatenate(flats, axis=1), (np.concatenate(alphas, axis=1) > 0.5).astype(np.uint8))
    lit, x = [], 0
    for f in flats:
        lit.append(joined[:, x:x + f.shape[1]])
        x += f.shape[1]
    out = list(zip(_trim_seam(lit), _trim_seam(alphas)))
    if any(two_stones(f, a) for f, a in out):
        return "two stones in one outline (the slab behind it, seen past a broken corner?)"
    if (bite := max(hollow(a) for _, a in out)) > C.OUTLINE_HOLLOW_MAX:
        return f"something stands in front of the slab (a stand or rack bites {bite:.0%} out of its outline)"
    return out


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


def shadowed_on_white(rgb, cover):
    """The canvas-size RGB laid on white where `cover` (0..1) is, with the soft navy shadow under it."""
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
    return shadowed_on_white(*_white_slabs(slabs))
