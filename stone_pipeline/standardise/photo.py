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
    slab: tuple = ()                                   # (squared RGB, alpha) of one slab: what a 3D viewer needs


def _finished(slabs, key, models, seed):
    solid, stand = layout.masks(slabs, key)
    return blend.blend(layout.compose(slabs, key), solid, stand, models.inpaint, seed)


def standardise(rgb, *, fmt, key, size=None, clean=None, watermarked=False, models=None, extra_views=False):
    """The finished images of one photo. fmt = slab | block | tile (the product's format); key = the product, it alone
    chooses the room; size = (length, width) in metres; clean = the already-cleaned copy of a watermarked source."""
    if watermarked:
        if clean is None:
            return Result(held="logo: no cleaned copy of this watermarked photo")
        rgb, reason = logos.remove(rgb, clean)
        if rgb is None:
            return Result(held=reason)
    if fmt in ("block", "tile"):
        made = scene.block(rgb, *models.cleaners) if fmt == "block" else scene.tile(rgb, models.lama, models.esr)
        return Result(kind=fmt, images={"main": made[0]})
    kind, confidence = kinds.classify(models.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return Result(held=f"not a slab photo: {kind} ({confidence:.2f})", kind=kind)
    slabs = layout.squared_slabs(rgb, *models.cleaners)
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
    if extra_views and kind != "close-up":
        for side in C.ANGLE_SIDES:
            img, solid, stand = views.angled(slabs, key, side)
            images[f"angle_{side}"] = blend.blend(img, solid, stand, models.inpaint, C.BLEND_SEED)
            images[f"white_angle_{side}"] = layout.on_white(slabs, turn=side)
        if kind == "slab":
            up = views.close_up(slabs[0][0], slabs[0][1], models.esr)
            if up is not None:
                images["close"] = up
    return Result(kind=kind, images=images, slab=slabs[0] if kind == "slab" else ())


def decode(data):
    """RGB of an encoded photo, at the working size; raises ValueError on an undecodable one."""
    bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("undecodable image")
    return layout.fit_working(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
