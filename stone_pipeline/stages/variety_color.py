"""Infer a variety's colour from its real PRODUCT IMAGE when the source supplied none.

A source like varsha/zucchi has no colour column and exotic names with no colour word, so a variety would
be born colourless and null its products' colour_id. Its de-watermarked product photo is the honest stone,
so we classify that photo's perceived colour, map it to a real Medusa colour, and propagate it to the
variety's tile/block mirrors (same stone). The colour re-derives when the product image changes (sha
differs), so a corrected/regenerated photo updates it instead of staying stuck. Only when there is no
product image AND no coloured sibling does the generic 'Natural' fallback remain.

NB: the colour comes from the PRODUCT image (the real scraped photo), never the generated variant icon --
an icon can be a stale/placeholder render that diverges from the actual stone.
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


# --- perceptual dominant colour ----------------------------------------------
# A stone is named for its saturated ACCENT, not its statistical average: a green quartzite with cream/gold
# veining medians to a warm tone, so a per-channel median mislabels it. Name a stone by its dominant
# CHROMATIC hue when it has a real coloured fraction; fall to the neutral lightness ramp only for a
# genuinely neutral (grey/white/cream) stone.
_CHROMA_S = 0.25             # a pixel is "coloured" (not neutral matrix) at or above this saturation
_CHROMA_MIN_FRACTION = 0.12  # the stone counts as "coloured" when this fraction of pixels are chromatic
_HUE_BINS = 24               # 15-degree hue buckets for the dominant-hue vote


def dominant_color(arr) -> str | None:
    """Perceived colour of a bg-removed stone image (HxWx4 or HxWx3 numpy array). None if too few opaque
    pixels. A stone with a real coloured fraction is named by the dominant hue of its CHROMATIC pixels (so a
    saturated green survives light veining); a genuinely neutral stone falls to the lightness ramp."""
    import numpy as np
    a = arr.reshape(-1, arr.shape[-1])
    opaque = (a[a[:, 3] > 200][:, :3] if a.shape[1] == 4 else a[:, :3]).astype(float)
    if len(opaque) < 50:
        return None
    rgbn = opaque / 255.0
    mx = rgbn.max(1); mn = rgbn.min(1); v = mx
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(mx > 0, (mx - mn) / mx, 0.0)
    chrom = (s >= _CHROMA_S) & (v > 0.15) & (v < 0.97)
    if int(chrom.sum()) >= 50 and float(chrom.mean()) >= _CHROMA_MIN_FRACTION:
        d = mx - mn
        r, g, b = rgbn[:, 0], rgbn[:, 1], rgbn[:, 2]
        h = np.zeros(len(rgbn))
        with np.errstate(divide="ignore", invalid="ignore"):
            nz = d > 1e-6
            i = (mx == r) & nz; h[i] = ((g[i] - b[i]) / d[i]) % 6
            i = (mx == g) & nz; h[i] = ((b[i] - r[i]) / d[i]) + 2
            i = (mx == b) & nz; h[i] = ((r[i] - g[i]) / d[i]) + 4
        h = (h * 60.0) % 360.0
        hc = h[chrom]
        hist, edges = np.histogram(hc, bins=_HUE_BINS, range=(0, 360))
        peak = int(hist.argmax()); lo, hi = edges[peak], edges[peak + 1]
        rep = np.median(opaque[chrom][(hc >= lo) & (hc < hi)], axis=0)
        return classify(tuple(int(x) for x in rep))
    return classify(tuple(int(x) for x in np.median(opaque, axis=0)))


def _color_from_array(arr) -> str | None:
    return dominant_color(arr)


def color_from_image_bytes(data: bytes) -> str | None:
    """Perceived colour of an encoded product image (bytes). None if undecodable / too few opaque pixels.
    Used by the image stage to colour a variety from its REAL product photo, not its generated icon."""
    try:
        import io

        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
    except Exception:
        return None
    return _color_from_array(arr)




def _fetch_product_image(url: str) -> bytes | None:
    """GET a product image's bytes from S3 by the key embedded in its url. None if boto3/creds/object are
    absent (CI/local) so callers no-op. The url is the improved product photo linked by the image stage."""
    if not url or ".amazonaws.com/" not in url:
        return None
    key = url.split(".amazonaws.com/", 1)[1].split("?", 1)[0]
    try:
        import boto3
        s3 = SETTINGS.s3
        client = boto3.Session(profile_name=s3.credentials_profile or None,
                               region_name=s3.region).client("s3")
        return client.get_object(Bucket=s3.bucket, Key=key)["Body"].read()
    except Exception:
        return None


def _identity(post: dict) -> tuple[str, str]:
    # canonical key both sides, so a slab and its mirror share identity despite accent/separator drift
    return (match_key(post.get("stone_type", "")), match_key(post.get("variant", "")))


def fill_colors(backbone_paths: list[Path] | None = None,
                reference_paths: list[Path] | None = None,
                product_images: dict[str, str] | None = None,
                fetch=None) -> dict:
    """Seed every variety's colour from its real PRODUCT IMAGE (`product_images`: backbone key ->
    de-watermarked product-photo url), then propagate by (type, name) to same-stone mirrors, leaving the
    pack's generic colour ('Natural') only where nothing is known. A variety RE-DERIVES when its product
    image changes (the image sha differs from the one last used), so a corrected/regenerated photo updates
    the colour instead of staying stuck. Edits `backbone_paths` in place; `reference_paths` widen the
    known-colour map READ-ONLY (so a mirror inherits an already-merged sibling's colour). `fetch` is the
    image byte-getter (injectable for tests). Returns counts."""
    from stone_pipeline.io import imagestore
    paths = backbone_paths or [c.backbone_path for c in __import__(
        "stone_pipeline.config.settings", fromlist=["CATEGORIES"]).CATEGORIES]
    fallback_color = active_pack().fallback_color   # the pack's generic colour ('Natural' for stone)
    product_images = product_images or {}
    fetch = fetch or _fetch_product_image
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

    from_image = 0
    by_sha: dict[str, str | None] = {}   # a photo shared across mirrors classifies once
    # 1. colour from the real product image -- (re)derived when blank OR the photo changed since last time
    for doc in docs.values():
        for post in posts(doc):
            key = post.get("key")
            url = product_images.get(key) if key else None
            sha = imagestore.sha_from_url(url) if url else None
            if not (is_blank(post) or (sha and sha != post.get("color_src_sha"))):
                continue                                 # keep the current colour: still valid + same photo
            if not (url and sha):
                continue                                 # no product image -> leave for propagate/floor
            if sha not in by_sha:
                data = fetch(url)
                by_sha[sha] = color_from_image_bytes(data) if data else None
            col = by_sha[sha]
            if col:
                post["color"] = [col]
                post["color_src_sha"] = sha              # so we only re-derive when the photo changes
                from_image += 1
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
    stats = {"from_image": from_image, "propagated": propagated, "fallback_natural": fallback}
    log.info("variety colours filled", extra={"extra_fields": stats})
    return stats
