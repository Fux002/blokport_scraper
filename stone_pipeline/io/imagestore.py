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
    products/standard/<view>/<src>/<hash>.jpg  a STANDARDISED image of that photo (deploy.standardise_publish):
                                            view = main (showroom), white, studio (side view), close
    products/standard/cutout/<src>/<hash>.webp|.json  the 3D slab of that photo: the cut-out and its outline
    products/standard/<view>|cutout/<src>/<listing hash>.*  a TILE listing's views and 3D tile, rendered from its
                                            variety's slab stone (standardise_publish.publish_tiles), named after
                                            the listing itself (listing_sha), not a photo: one photo can show several
                                            tile sizes
    products/standard/tile/<src>/<listing hash>.json  what that listing's files were made from (written last)
    products/standard/assigned/<src>/<hash>.json  a published photo listings of several stones show: the SKUs it is for
    products/standard/conflicts/<src>.json  the produce's conflict set: {sha: [handles]} of its emitted products
    standardise-test/removed/<SKU>.json     a listing the owner removed (SKU percent-encoded)
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import quote, unquote

from stone_pipeline.config.settings import ENV_SEGMENT, STANDARDISE

IMPROVED_SUBDIR = "improved"                 # the treated-image folder name (also settings.improved_subdir)
SCRAPED_SUBDIR = "scraped"
DISCARDED_SUBDIR = "discarded"               # non-stone images the classifier rejected (spec sheets, logos)
ENHANCED_SUBDIR = "enhanced"                 # per-image "done" markers the GPU reprocess writes (see below)
STANDARD_SUBDIR = "standard"                 # the standardised photos, one folder per view
STANDARD_VIEWS = ("main", "white", "studio", "close")
CUTOUT_SUBDIR = "cutout"                     # under standard/: the 3D slab files, beside the views
TILE_SUBDIR = "tile"                         # under standard/: what each rendered tile listing was made from
ASSIGNED_SUBDIR = "assigned"                 # under standard/: who a published conflict photo is assigned to
CONFLICTS_SUBDIR = "conflicts"               # under standard/: the produce's conflict set, one file per source

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


def standard_prefix(view: str) -> str:
    return f"{_PRODUCTS}/{STANDARD_SUBDIR}/{view}/"


def standard_key(source: str, sha256: str, view: str) -> str:
    """A standardised view of one photo. The view is a folder, so the key still ends <sha>.<ext> like every image key."""
    return f"{standard_prefix(view)}{source}/{sha256}.jpg"


def cutout_prefix() -> str:
    return standard_prefix(CUTOUT_SUBDIR)


def cutout_key(source: str, sha256: str, ext: str) -> str:
    """One of the two 3D slab files of a photo: ext = webp (the cut-out) or json (its outline)."""
    return f"{cutout_prefix()}{source}/{sha256}.{ext}"


def listing_sha(handle: str) -> str:
    """The name a listing's own standardised files carry (a rendered tile's): a hash of its handle, shaped like a photo's
    content hash so every key still ends <sha>.<ext>, and apart from every photo's (the 'listing:' prefix)."""
    return hashlib.sha256(f"listing:{handle}".encode()).hexdigest()


def tile_record_key(source: str, sha256: str) -> str:
    return f"{standard_prefix(TILE_SUBDIR)}{source}/{sha256}.json"


def cutout_meta_url(cutout_url: str) -> str:
    """The outline file beside a cut-out: same name, .json."""
    return cutout_url[:-len(".webp")] + ".json"


def sku_file(sku: str) -> str:
    """A record's file name for a product: its SKU percent-encoded (supplier SKUs carry slashes, spaces and plus signs,
    MAR-P/02/16), so each names exactly one key directly in its folder."""
    return f"{quote(sku, safe='')}.json"


def sku_of(key: str, prefix: str) -> str:
    """The SKU a record's key (prefix + sku_file) names."""
    return unquote(key[len(prefix):-len(".json")])


def skus_in(keys, prefix: str) -> set[str]:
    """The SKUs of the records (sku_file) directly under `prefix` among `keys`."""
    return {sku_of(k, prefix) for k in keys if k.startswith(prefix) and k.endswith(".json")}


def owner_removed_prefix() -> str:
    """The listings the owner removed (deploy.standardise_decisions), one record per SKU: the photo process never works on
    them and the image stage links nothing standardised to them."""
    return f"{ENV_SEGMENT}/{STANDARDISE.TEST_PREFIX}/{STANDARDISE.REMOVED_DIR}/"


def owner_removed_key(sku: str) -> str:
    return f"{owner_removed_prefix()}{sku_file(sku)}"


def owner_removed_skus(keys) -> set[str]:
    return skus_in(keys, owner_removed_prefix())


def conflicts_prefix() -> str:
    """Beside the published views: the produce's conflict set per source, <source>.json = {sha: [listing handles]} of the
    photos its emitted products of several stones show (stages.images.link_standard). The one set: the image stage
    applies it, the photo process, the owner's decisions and the review page read it."""
    return f"{_PRODUCTS}/{STANDARD_SUBDIR}/{CONFLICTS_SUBDIR}/"


def conflicts_key(source: str) -> str:
    return f"{conflicts_prefix()}{source}.json"


def conflicts_source(key: str) -> Optional[str]:
    """The source a conflict set's key is of, None for any other key."""
    name = key[len(conflicts_prefix()):] if key.startswith(conflicts_prefix()) else ""
    return name.removesuffix(".json") if name.endswith(".json") and "/" not in name else None


def assigned_prefix() -> str:
    """Beside the published views: for each published photo several stones' listings show (a conflict), the SKUs the
    owner assigned it to (deploy.standardise_publish); the image stage links it for those products only."""
    return f"{_PRODUCTS}/{STANDARD_SUBDIR}/{ASSIGNED_SUBDIR}/"


def assigned_key(source: str, sha256: str) -> str:
    return f"{assigned_prefix()}{source}/{sha256}.json"


def assigned_photo(key: str) -> Optional[tuple[str, str]]:
    """(source, sha) of an assignment file's key, None for any other key."""
    rest = key[len(assigned_prefix()):] if key.startswith(assigned_prefix()) else ""
    source, _, name = rest.partition("/")
    sha = name.removesuffix(".json")
    return (source, sha) if source and name.endswith(".json") and re.fullmatch(r"[0-9a-f]{64}", sha) else None


def sha_from_url(url: str) -> Optional[str]:
    """The content sha256 embedded in a hosted image url/key (.../<site>/<sha>.<ext>), or None. Used by
    Stage 7 to map a linked improved url back to its content identity for the discard-set lookup."""
    if not url:
        return None
    m = _SHA_RE.search(url)
    return m.group(1).lower() if m else None


