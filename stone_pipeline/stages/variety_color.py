"""Infer a variety's colour from its generated texture when the source supplied none.

A source like zucchi has no colour column and exotic names with no colour word, so a variety would be
born colourless and null its products' colour_id. But the variant's FLUX→BEN2 texture is a clean,
uniform render of the actual stone, so its dominant colour is the honest colour. We read that, map it
to a real Medusa colour, and propagate it to the variety's tile/block mirrors (same stone). Only when
there is no texture AND no coloured sibling does the generic 'Natural' fallback remain.
"""

from __future__ import annotations

import colorsys
import json
from pathlib import Path

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.core.text import match_key

log = logfmt.get_logger("variety_color")


def classify(rgb: tuple[int, int, int]) -> str:
    """Median stone RGB -> a Medusa colour name. Natural stone is mostly low-saturation, so the
    greyscale lightness ramp (Black/Grey/White, warm->Cream) carries most cases; saturated stones
    fall to a hue bucket."""
    r, g, b = (x / 255 for x in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h *= 360
    warm = rgb[0] > rgb[2] + 12
    # Three saturation bands: a pure-neutral stone (s<0.10) or a washed-out LIGHT stone (mildly
    # saturated but very bright) reads as greyscale; everything else keeps its hue even when muted,
    # so a sage-green or dusty-blue stone isn't flattened to Grey.
    if s < 0.10 or (s < 0.25 and v > 0.80):       # greyscale / washed-light family
        if v < 0.27:
            return "Black"
        if v < 0.70:
            return "Grey"
        return "Cream" if warm else "White"
    if h < 20 or h >= 345:
        return "Red"
    if h < 45:
        return "Brown" if v < 0.55 else ("Gold" if s > 0.4 else "Beige")
    if h < 70:
        return "Yellow" if v > 0.6 else "Brown"
    if h < 170:
        return "Green"
    if h < 255:
        return "Blue"
    if h < 290:
        return "Purple"
    return "Pink"


# Every colour name classify() above can emit. Kept beside it so the pack-value existence gate
# (reference.loaders.load_all) can assert each is a real Medusa colour in attributes.csv -- a colour renamed
# or removed in Medusa then fails loud at load instead of classify() silently emitting an unresolvable one.
# MUST mirror the return values of classify().
CLASSIFIABLE_COLORS = frozenset({"Black", "Grey", "White", "Cream", "Red", "Brown",
                                 "Gold", "Beige", "Yellow", "Green", "Blue", "Purple", "Pink"})


def color_from_texture(path: Path) -> str | None:
    """Dominant colour of the OPAQUE pixels of a bg-removed texture, classified. None if unreadable
    or too few opaque pixels (PIL/numpy are optional deps; absence just means no inference)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        arr = np.asarray(Image.open(path).convert("RGBA"))
    except Exception:
        return None
    opaque = arr[arr[..., 3] > 200][:, :3]
    if len(opaque) < 50:
        return None
    return classify(tuple(int(x) for x in np.median(opaque, 0)))


def _texture_dir() -> Path:
    return SETTINGS.paths.workspace_root / "image_pipeline" / "to_upload"


def _identity(post: dict) -> tuple[str, str]:
    # canonical key both sides, so a slab and its mirror share identity despite accent/separator drift
    return (match_key(post.get("stone_type", "")), match_key(post.get("variant", "")))


def fill_colors(backbone_paths: list[Path] | None = None, texture_dir: Path | None = None,
                reference_paths: list[Path] | None = None) -> dict:
    """Give every colour-less variety (color empty or ['Natural']) a real colour from its texture,
    then propagate by (type, name) to mirrors, leaving 'Natural' only where nothing is known. Edits
    only `backbone_paths` in place. `reference_paths` are read READ-ONLY purely to widen the
    known-colour map -- so a tile/block mirror whose slab sibling already lives in the merged main
    backbone (not in this run's additions) still inherits the slab's colour instead of falling to
    Natural. Returns counts. Safe no-op when nothing is colour-less."""
    paths = backbone_paths or [c.backbone_path for c in __import__(
        "stone_pipeline.config.settings", fromlist=["CATEGORIES"]).CATEGORIES]
    tdir = texture_dir or _texture_dir()
    fallback_color = active_pack().fallback_color   # the pack's generic colour ('Natural' for stone)
    docs = {p: json.loads(Path(p).read_text(encoding="utf-8")) for p in paths if Path(p).exists()}
    ref_docs = [json.loads(Path(p).read_text(encoding="utf-8"))
                for p in (reference_paths or []) if Path(p).exists()]

    def is_blank(post) -> bool:
        c = post.get("color")
        return bool(post.get("key")) and (not c or c == [fallback_color])

    def posts(doc):
        # a backbone file is {"posts": [...]}; an additions file is a bare [...] list of posts. Both
        # are edited in place, and json.dumps(doc) below preserves whichever shape it was.
        return doc if isinstance(doc, list) else doc.get("posts", [])

    from_texture = 0
    # 1. texture-derived colour
    for doc in docs.values():
        for post in posts(doc):
            if not is_blank(post):
                continue
            tpath = tdir / f"{post['key']}.png"
            col = color_from_texture(tpath) if tpath.exists() else None
            if col:
                post["color"] = [col]
                from_texture += 1
    # 2. propagate a known colour to same-stone mirrors -- learn from BOTH the docs being edited and
    #    the read-only reference backbones (already-merged siblings).
    known: dict[tuple[str, str], list[str]] = {}
    for doc in list(docs.values()) + ref_docs:
        for post in posts(doc):
            c = post.get("color")
            if post.get("key") and c and c != [fallback_color]:
                known.setdefault(_identity(post), c)
    propagated = 0
    for doc in docs.values():
        for post in posts(doc):
            if is_blank(post):
                c = known.get(_identity(post))
                if c:
                    post["color"] = list(c)
                    propagated += 1
    # 3. anything still colour-less -> the generic fallback
    fallback = 0
    for doc in docs.values():
        for post in posts(doc):
            if is_blank(post):
                post["color"] = [fallback_color]
                fallback += 1
    for p, doc in docs.items():
        Path(p).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    stats = {"from_texture": from_texture, "propagated": propagated, "fallback_natural": fallback}
    log.info("variety colours filled", extra={"extra_fields": stats})
    return stats
