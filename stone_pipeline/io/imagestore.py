"""Single source of truth for the product-image S3 layout.

The manifest key and the raw / scraped / improved path conventions live HERE and nowhere else, so the
three consumers -- images.py (links products to treated images), stages/treat.py (treats raws into
improved + repoints the manifest), and deploy/reprocess_source.py (batch treat) -- never each hard-code
the layout. Change the layout once, in this file. No em dashes (design principle 2).

Layout (under the env segment, e.g. dev/):
    products/_manifest.json                 source_url -> hosted S3 url (the imageproc manifest)
    products/<src>/<hash>.jpg               a raw image staged in s3 mode (no keep_scraped)
    products/scraped/<src>/<hash>.jpg       a raw ORIGINAL kept alongside processing (keep_scraped)
    products/improved/<src>/<hash>.jpg      the TREATED image (enhance + upscale + compress; de-wm)
"""

from __future__ import annotations

import re
from typing import Optional

from stone_pipeline.config.settings import ENV_SEGMENT

IMG_EXT = (".jpg", ".jpeg", ".png")
IMPROVED_SUBDIR = "improved"                 # the treated-image folder name (also settings.improved_subdir)
SCRAPED_SUBDIR = "scraped"
DISCARDED_SUBDIR = "discarded"               # non-stone images the classifier rejected (spec sheets, logos)
ENHANCED_SUBDIR = "enhanced"                 # per-image "done" markers the GPU reprocess writes (see below)

_PRODUCTS = f"{ENV_SEGMENT}/products"
MANIFEST_KEY = f"{_PRODUCTS}/_manifest.json"
MANIFEST_BACKUP_KEY = f"{_PRODUCTS}/_manifest.backup.json"
IMPROVED_MARKER = f"/products/{IMPROVED_SUBDIR}/"   # a treated image's S3 url/key contains this substring
DISCARDED_PREFIX_ALL = f"{_PRODUCTS}/{DISCARDED_SUBDIR}/"  # list this to load the whole discard set

# "Done" markers, written ONLY by the GPU reprocess (deploy/reprocess_source), one per image it handles:
# an ENHANCED marker when it enhances, a DISCARDED marker when it rejects. These are the incremental signal
# for auto-enhance -- an image "needs processing" iff it is in scraped/ but has NEITHER marker. We CANNOT
# use improved/ presence for this: produce on the torch-free :core writes a raw re-encode into improved/
# without enhancing, so improved/ presence does not mean "enhanced". Only the GPU writes these markers.

# every image object is content-addressed as <site>/<sha256>.<ext>, so the sha is recoverable from any
# hosted url or key -- the one identity that the reprocess (filename) and Stage 7 (improved url) share.
_SHA_RE = re.compile(r"([0-9a-f]{64})\.[a-z0-9]+$", re.IGNORECASE)


def raw_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{source}/"


def scraped_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{SCRAPED_SUBDIR}/{source}/"


def improved_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{IMPROVED_SUBDIR}/{source}/"


def discarded_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{DISCARDED_SUBDIR}/{source}/"


def discarded_key(source: str, sha256: str) -> str:
    """The per-image discard marker key. One object per discarded image (content-addressed), so the many
    parallel reprocess slices write without ever racing a shared file. Body = {reason, score, classifier}."""
    return f"{_PRODUCTS}/{DISCARDED_SUBDIR}/{source}/{sha256}.json"


def enhanced_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{ENHANCED_SUBDIR}/{source}/"


def enhanced_key(source: str, sha256: str) -> str:
    """The per-image ENHANCED marker key (the GPU reprocess writes it after enhancing an image). Marks the
    image "done" for incremental auto-enhance, distinct from produce's raw re-encode in improved/."""
    return f"{_PRODUCTS}/{ENHANCED_SUBDIR}/{source}/{sha256}.txt"


def sha_from_url(url: str) -> Optional[str]:
    """The content sha256 embedded in a hosted image url/key (.../<site>/<sha>.<ext>), or None. Used by
    Stage 7 to map a linked improved url back to its content identity for the discard-set lookup."""
    if not url:
        return None
    m = _SHA_RE.search(url)
    return m.group(1).lower() if m else None


def parse_raw_key(key: str) -> tuple[str, str] | None:
    """(source, filename) from a raw products key, or None if it is already improved, the manifest, or
    not a product image. Handles both raw layouts: products/<src>/<f> and products/scraped/<src>/<f>."""
    if not key.lower().endswith(IMG_EXT):
        return None
    parts = key.split("/")
    if "products" not in parts:
        return None
    rest = parts[parts.index("products") + 1:]
    if not rest or rest[0] == IMPROVED_SUBDIR:       # already treated
        return None
    if rest[0] == SCRAPED_SUBDIR and len(rest) >= 3:
        return rest[1], rest[-1]
    if len(rest) >= 2:
        return rest[0], rest[-1]
    return None


def improved_key(raw_key: str) -> str:
    """The improved/ S3 key for a raw key, via the SAME path transform the manifest repoint uses, so the
    treated object and the repointed URL can never disagree (and any sub-path is preserved)."""
    scraped = f"/products/{SCRAPED_SUBDIR}/"
    if scraped in raw_key:
        return raw_key.replace(scraped, f"/products/{IMPROVED_SUBDIR}/", 1)
    return raw_key.replace("/products/", f"/products/{IMPROVED_SUBDIR}/", 1)


def raw_to_improved_url(url: str, source: str) -> str:
    """Repoint a manifest VALUE (a full hosted url) for `source` from its raw location to improved/.
    A url already under improved/<src>/ is returned unchanged (idempotent)."""
    improved = f"/products/{IMPROVED_SUBDIR}/{source}/"
    if improved in url:
        return url
    for marker in (f"/products/{source}/", f"/products/{SCRAPED_SUBDIR}/{source}/"):
        if marker in url:
            return url.replace(marker, improved, 1)
    return url
