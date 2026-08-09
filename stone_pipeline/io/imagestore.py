"""Single source of truth for the product-image S3 layout.

The manifest key and the raw / scraped / improved path conventions live HERE and nowhere else, so the
consumers -- images.py (links products to treated images) and deploy/reprocess_source.py (batch treat
raws into improved + write the enhanced markers) -- never each hard-code the layout. Change the layout
once, in this file. No em dashes (design principle 2).

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

IMPROVED_SUBDIR = "improved"                 # the treated-image folder name (also settings.improved_subdir)
SCRAPED_SUBDIR = "scraped"
DISCARDED_SUBDIR = "discarded"               # non-stone images the classifier rejected (spec sheets, logos)
ENHANCED_SUBDIR = "enhanced"                 # per-image "done" markers the GPU reprocess writes (see below)

_PRODUCTS = f"{ENV_SEGMENT}/products"
MANIFEST_KEY = f"{_PRODUCTS}/_manifest.json"
MANIFEST_BACKUP_KEY = f"{_PRODUCTS}/_manifest.backup.json"
IMPROVED_MARKER = f"/products/{IMPROVED_SUBDIR}/"   # a treated image's S3 url/key contains this substring
DISCARDED_PREFIX_ALL = f"{_PRODUCTS}/{DISCARDED_SUBDIR}/"  # list this to load the whole discard set
ENHANCED_PREFIX_ALL = f"{_PRODUCTS}/{ENHANCED_SUBDIR}/"    # list this for the whole enhanced-marker set (publish gate)

# "Done" markers, one per image: an ENHANCED marker when a source's configured image pipeline COMPLETED for
# that image, a DISCARDED marker when the classifier rejected it. They are the publish gate (require_enhanced)
# AND the incremental signal for auto-enhance -- an image "needs the GPU" iff it is in scraped/ but has NEITHER
# marker. Who writes the ENHANCED marker follows the PER-SOURCE page settings: the :core produce writes it when
# the CPU size-reduce is the whole job (page enhance=off AND watermarked=off), so a resize-only source needs no
# GPU trip; the GPU reprocess writes it after it upscales/de-watermarks a source the page marks enhance=on or
# watermarked=on. We CANNOT infer "done" from improved/ presence alone: for an enhance=on source, :core writes a
# pre-upscale re-encode into improved/ that is NOT yet complete -- only the marker means the configured job ran.

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


