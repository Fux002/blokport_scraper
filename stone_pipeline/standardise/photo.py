"""One scraped photo to its finished images: the single path of the standardiser. Every photo ends finished or held with
a reason, one bad photo never stops the run (the caller records the hold)."""
from dataclasses import dataclass, field

import cv2
import numpy as np

from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.standardise import blend, closeup, kinds, layout, scene, studio, views


@dataclass
class Result:
    held: str = ""                                     # why this photo is not published; empty = finished
    kind: str = ""                                     # what it was classified as
    images: dict = field(default_factory=dict)         # name -> RGB array at the canvas size
    slab: tuple = ()                                   # (squared RGB, alpha) of one whole slab: what a 3D viewer needs
    chosen: tuple = ()                                 # (which, of how many) slabs found `slab` is: what its close-up is of
    changed: bool = False                              # a whole slab this version draws differently from the rule
                                                       # before OUTLINE_CHECKED (layout.drawn_differently)
    unchanged: bool = False                            # a recheck found it drawn the same: nothing drawn


class NoCloseUp(Exception):
    """This photo has no close-up for a reason of its own: not the slabs its run found in it now, its logo cannot be
    trusted, its run refused it, it is off the product sheet, it cannot be decoded."""


def _finished(slabs, key, models, seed):
    solid, stand = layout.masks(slabs, key)
    return blend.blend(layout.compose(slabs, key), solid, stand, models.inpaint, seed)


def standardise(full, *, fmt, key, size=None, models=None, extra_views=False, edits=None, redo=None):
    """The finished images of one photo, `full` at its full resolution (read). fmt = slab | block | tile (the product's
    format); key = the product, it alone chooses the room; size = (length, width) in metres; edits = a function giving
    where `full` itself is not as shot (views.close_up), called only when a close-up is made. A logo or overlay left on
    the stone is the judge's finding (leftover_mark) on the result, which the hosted clean step removes from the photo;
    no step here looks for one. redo: the photo's stored result is worked on again: "recheck" (a whole slab made under an
    older outline rule: unchanged, nothing drawn, when this version draws it the same) or "complete" (an open book made
    before its halves had a slab: drawn in full); either way never a close-up instead (held with the reason)."""
    rgb = layout.fit_working(full)
    if fmt in ("block", "tile"):
        made = scene.block(rgb, *models.cleaners) if fmt == "block" else scene.tile(rgb, models.lama, models.esr)
        return Result(kind=fmt, images={"main": made[0]})
    kind, confidence = kinds.classify(models.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return Result(held=f"not a slab photo: {kind} ({confidence:.2f})", kind=kind)
    found = layout.slab_outline(rgb, models.seg, models.det, size)
    changed = not isinstance(found, str) and layout.drawn_differently(found)
    if redo == "recheck" and not isinstance(found, str) and not changed:
        return Result(kind=kind, unchanged=True)
    flats = found if isinstance(found, str) else layout.flats_from(rgb, found, models.lama)
    slabs = flats if isinstance(flats, str) else layout.enlarged(flats, models.esr)
    if isinstance(slabs, str):
        if redo:
            return Result(held=slabs, kind=kind)
        slabs_reason, slabs = slabs, closeup.closeup(rgb, size, *models.cleaners)
        if isinstance(slabs, str):
            return Result(held=f"{slabs_reason}; {slabs}", kind=kind)
        kind, changed = "close-up", False
    elif len(slabs) == 2:
        kind = "open book"
    else:
        kind = "slab"
    images = {"main": _finished(slabs, key, models, C.BLEND_SEED)}
    images["white"] = layout.on_white(slabs)
    if kind != "close-up":                              # Blokport's studio scene: the slab on its chrome feet
        images["studio"] = studio.place(slabs)
    i = whole_index([a for _, a in slabs], kind)
    whole, chosen = ((), ()) if i is None else (slabs[i], (i, len(slabs)))
    if extra_views and whole:
        try:
            up = views.close_up(flats[i], full, edits() if edits else None)   # the same slab, at the photo's own pixels
        except NoCloseUp:                               # the close-up's own input is missing: the other views stand
            up = None
        if up is not None:
            images["close"] = up
    return Result(kind=kind, images=images, slab=whole, chosen=chosen, changed=changed)


def whole_index(alphas, kind):
    """Which of the slabs (their outlines as drawn: layout.drawn_alphas) is the one whole real slab the 3D slab and the
    close view are made from: a single slab's own; of an open book, one of its halves (each is a whole slab: the one
    with more stone in its outline, the left on a tie); None for a close-up, which shows only part of a slab."""
    if kind == "open book":
        sums = [float(a.sum()) for a in alphas]
        return sums.index(max(sums))                    # the first of equals: the left half
    return 0 if kind == "slab" else None


def close_view(full, *, size, models, edits=None, chosen, saved):
    """The close-up alone (views.close_up) of a photo its run made a whole slab of (a single slab, or an open book's whole
    half), or None when it gets none: the steps of standardise up to the squared slabs, none of what only the other views
    need, the close-up of the slab its 3D slab shows (run_slab). NoCloseUp when the slabs found now do not tell which one
    that is."""
    rgb = layout.fit_working(full)
    flats = layout.flat_slabs(rgb, models.seg, models.det, models.lama, size=size)
    if isinstance(flats, str):
        raise NoCloseUp(f"no whole slab now: {flats}")
    return views.close_up(flats[run_slab(flats, chosen, saved)], full, edits)


def run_slab(flats, chosen, saved):
    """Which of the slabs found now is the one the photo's run made its 3D slab of. chosen: (which, of how many) its run
    recorded (Result.chosen); None for a result made before that was recorded, which is decided against saved, the
    (RGB, alpha) of the <sha>.slab.png it saved: the one slab found now its size as drawn (CLOSEUP_SLAB_SIZE_TOL: a
    single slab is drawn about twice an open book's half, so a slab count that changed shows) and showing its stone
    (CLOSEUP_SLAB_MATCH_MIN, by CLOSEUP_SLAB_MATCH_MARGIN over an open book's other half, its mirror image). Never a
    guess: NoCloseUp when the slabs found now do not tell."""
    if chosen is not None:
        i, of = chosen
        if len(flats) != of:
            raise NoCloseUp(f"its run found {of} slabs in it, now {len(flats)}")
        return i
    rgb, alpha = saved
    tol = C.CLOSEUP_SLAB_SIZE_TOL
    sized = [i for i, a in enumerate(layout.drawn_alphas(flats))
             if abs(a.shape[1] / alpha.shape[1] - 1) <= tol and abs(a.shape[0] / alpha.shape[0] - 1) <= tol]
    if not sized:
        raise NoCloseUp(f"none of the {len(flats)} slabs found now is the size of the one its run saved")
    shown = _structure(rgb, alpha, alpha.shape[1::-1])
    scores = sorted(((_likeness(shown, _structure(flats[i].rgb, flats[i].alpha, alpha.shape[1::-1])), i) for i in sized),
                    reverse=True)
    best, i = scores[0]
    if best < C.CLOSEUP_SLAB_MATCH_MIN:
        raise NoCloseUp(f"no slab found now shows the stone its run saved (likeness {best:.2f})")
    if len(scores) > 1 and best - scores[1][0] < C.CLOSEUP_SLAB_MATCH_MARGIN:
        raise NoCloseUp(f"which half its run saved is not told apart (likeness {best:.2f} and {scores[1][0]:.2f})")
    return i


def _structure(rgb, alpha, like):
    """The stone's structure (its veins and patches, not its light or finest detail) at CLOSEUP_SLAB_MATCH_WIDTH px across
    in the shape of `like` (width, height), and where it is stone: what a squared slab and its lit, enlarged copy share."""
    w = C.CLOSEUP_SLAB_MATCH_WIDTH
    size = (w, max(1, round(w * like[1] / like[0])))
    g = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32), size, interpolation=cv2.INTER_AREA)
    return g - cv2.GaussianBlur(g, (0, 0), C.CLOSEUP_SLAB_MATCH_SMOOTH), cv2.resize(alpha, size, interpolation=cv2.INTER_AREA) > 0.5


def _likeness(a, b):
    """The correlation of two structures (_structure) where both are stone; 0 when either shows none."""
    (ga, ma), (gb, mb) = a, b
    inside = ma & mb
    x, y = ga[inside] - ga[inside].mean(), gb[inside] - gb[inside].mean()
    norm = float(np.sqrt((x * x).sum() * (y * y).sum()))
    return float((x * y).sum()) / norm if norm > 0 else 0.0


def retouch_fills(retouched, made_from):
    """Where a retouched copy is not the photo it was made from (0/1 at its size): the retouch's fills, told from the JPEG
    re-encoding's own small differences (CLOSEUP_RETOUCH_DIFF). A retouch that only cropped the photo clear of its marks
    (another size) filled nothing."""
    if retouched.shape != made_from.shape:
        return np.zeros(retouched.shape[:2], np.uint8)
    # Luma only: a re-encode's chroma subsampling moves colour along every vein, a fill changes the stone's own light.
    luma = lambda rgb: cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    off = np.abs(luma(retouched) - luma(made_from))
    return (cv2.GaussianBlur(off, (0, 0), C.CLOSEUP_RETOUCH_SMOOTH) > C.CLOSEUP_RETOUCH_DIFF).astype(np.uint8)


def read(data):
    """RGB of an encoded photo at its full resolution; raises ValueError on an undecodable one."""
    bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("undecodable image")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def decode(data):
    """RGB of an encoded photo, at the working size; raises ValueError on an undecodable one."""
    return layout.fit_working(read(data))
