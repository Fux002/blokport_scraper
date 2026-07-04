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

from stone_pipeline.config.settings import ENV_SEGMENT

IMG_EXT = (".jpg", ".jpeg", ".png")
IMPROVED_SUBDIR = "improved"                 # the treated-image folder name (also settings.improved_subdir)
SCRAPED_SUBDIR = "scraped"

_PRODUCTS = f"{ENV_SEGMENT}/products"
MANIFEST_KEY = f"{_PRODUCTS}/_manifest.json"
MANIFEST_BACKUP_KEY = f"{_PRODUCTS}/_manifest.backup.json"
IMPROVED_MARKER = f"/products/{IMPROVED_SUBDIR}/"   # a treated image's S3 url/key contains this substring


def raw_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{source}/"


def scraped_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{SCRAPED_SUBDIR}/{source}/"


def improved_prefix(source: str) -> str:
    return f"{_PRODUCTS}/{IMPROVED_SUBDIR}/{source}/"


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
