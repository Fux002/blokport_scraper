"""One scraped photo to its finished images: the single path of the standardiser. Every photo ends finished or held with
a reason, one bad photo never stops the run (the caller records the hold)."""
from dataclasses import dataclass, field

import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import blend, closeup, kinds, layout, logos, scene, studio, views


@dataclass
class Result:
    held: str = ""                                     # why this photo is not published; empty = finished
    kind: str = ""                                     # what it was classified as
    images: dict = field(default_factory=dict)         # name -> RGB array at the canvas size
    slab: tuple = ()                                   # (squared RGB, alpha) of one whole slab: what a 3D viewer needs
    chosen: tuple = ()                                 # (which, of how many) slabs found `slab` is: what its close-up is of


class NoCloseUp(Exception):
    """This photo has no close-up for a reason of its own: not the slabs its run found in it now, its logo cannot be
    trusted, its run refused it, it is off the product sheet, it cannot be decoded."""


def _finished(slabs, key, models, seed):
    solid, stand = layout.masks(slabs, key)
    return blend.blend(layout.compose(slabs, key), solid, stand, models.inpaint, seed)


def _work_copy(full, clean, watermarked):
    """(the photo at the working size, its logo taken off when it has one, "") or (None, why the logo cannot be)."""
    rgb = layout.fit_working(full)
    if not watermarked:
        return rgb, ""
    if clean is None:
        return None, "logo: no cleaned copy of this watermarked photo"
    return logos.remove(rgb, clean)


def standardise(full, *, fmt, key, size=None, clean=None, watermarked=False, models=None, extra_views=False, edits=None):
    """The finished images of one photo, `full` at its full resolution (read). fmt = slab | block | tile (the product's
    format); key = the product, it alone chooses the room; size = (length, width) in metres; clean = the already-cleaned
    copy of a watermarked source; edits = a function giving where `full` itself is not as shot (views.close_up), called
    only when a close-up is made."""
    rgb, reason = _work_copy(full, clean, watermarked)
    if rgb is None:
        return Result(held=reason)
    if fmt in ("block", "tile"):
        made = scene.block(rgb, *models.cleaners) if fmt == "block" else scene.tile(rgb, models.lama, models.esr)
        return Result(kind=fmt, images={"main": made[0]})
    kind, confidence = kinds.classify(models.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return Result(held=f"not a slab photo: {kind} ({confidence:.2f})", kind=kind)
    flats = layout.flat_slabs(rgb, models.seg, models.det, models.lama, size=size)
    slabs = flats if isinstance(flats, str) else layout.enlarged(flats, models.esr)
    if isinstance(slabs, str):
        slabs_reason, slabs = slabs, closeup.closeup(rgb, size, *models.cleaners)
        if isinstance(slabs, str):
            return Result(held=f"{slabs_reason}; {slabs}", kind=kind)
        kind = "close-up"
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
    return Result(kind=kind, images=images, slab=whole, chosen=chosen)


def whole_index(alphas, kind):
    """Which of the slabs (their outlines as drawn: layout.drawn_alphas) is the one whole real slab the 3D slab and the
    close view are made from: a single slab's own; of an open book, one of its halves (each is a whole slab: the one
    with more stone in its outline, the left on a tie); None for a close-up, which shows only part of a slab."""
    if kind == "open book":
        sums = [float(a.sum()) for a in alphas]
        return sums.index(max(sums))                    # the first of equals: the left half
    return 0 if kind == "slab" else None


def close_view(full, *, size, models, clean=None, watermarked=False, edits=None, chosen=None):
    """The close-up alone (views.close_up) of a photo its run made a whole slab of (a single slab, or an open book's whole
    half), or None when it gets none: the steps of standardise up to the squared slabs, none of what only the other views
    need. chosen: (which, of how many) slabs its run recorded (Result.chosen), so the close-up is of the slab its 3D slab
    shows; None for a result made before that was recorded: the run's rule again, on the slabs as it drew them. NoCloseUp
    when its logo cannot be trusted or it no longer gives the slabs its run found."""
    rgb, reason = _work_copy(full, clean, watermarked)
    if rgb is None:
        raise NoCloseUp(reason)
    flats = layout.flat_slabs(rgb, models.seg, models.det, models.lama, size=size)
    if isinstance(flats, str):
        raise NoCloseUp(f"no whole slab now: {flats}")
    if chosen is None:
        return views.close_up(flats[whole_index(layout.drawn_alphas(flats), "open book" if len(flats) == 2 else "slab")],
                              full, edits)
    i, of = chosen
    if len(flats) != of:
        raise NoCloseUp(f"its run found {of} slabs in it, now {len(flats)}")
    return views.close_up(flats[i], full, edits)


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
