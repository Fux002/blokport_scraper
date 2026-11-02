"""Which product a scraped photo belongs to: the GPU job works per photo and has no row, so it reads the emitted product
sheet (each product's handle, category and size) and, beside it, the photo list (each product's own photos in its order:
the sheet's image columns carry only the chosen photo's views once it is published). A photo used by several products
takes the first in sheet order, so the choice is stable across runs."""
import csv
import io
import json
import re

from stone_pipeline.config import settings
from stone_pipeline.io import imagestore

# A hosted photo (/products/<scraped|improved>/<source>/<sha>.<ext>) or a published standardised view of one
# (/products/standard/<view>/<source>/<sha>.jpg, the photo's own sha), read as that photo.
_URL = re.compile(r"/products/(?:" + re.escape(imagestore.STANDARD_SUBDIR) + r"/[a-z]+/|[a-z]+/)([a-z_]+)/([0-9a-f]{64})\.")
_SOURCE, _SHA = re.compile(r"[a-z_]+"), re.compile(r"[0-9a-f]{64}")


def _size(row):
    try:
        length, width = float(row["Product Length"]), float(row["Product Width"])
    except (KeyError, TypeError, ValueError):
        return None
    return (length, width) if length > 0 and width > 0 else None


def photo_refs(urls):
    """[[source, sha], ...] of a product's hosted photo urls, in their order, each once (what the photo list holds)."""
    found = (_URL.search(url or "") for url in urls)
    return [list(ref) for ref in dict.fromkeys((m.group(1), m.group(2)) for m in found if m)]


def parse_photos(text):
    """{handle: [(source, sha), ...]} of the photo list's text. ValueError on anything else: a list read wrong would
    send the wrong photos to the GPU and the judge, so it is never guessed around."""
    value = json.loads(text)                          # JSONDecodeError is a ValueError
    if not isinstance(value, dict):
        raise ValueError("the photo list is not an object of listings")
    out = {}
    for handle, refs in value.items():
        if not handle or not isinstance(refs, list):
            raise ValueError(f"the photo list's entry {handle!r} is not a listing with a list of photos")
        for ref in refs:
            if not (isinstance(ref, list) and len(ref) == 2 and all(isinstance(v, str) for v in ref)
                    and _SOURCE.fullmatch(ref[0]) and _SHA.fullmatch(ref[1])):
                raise ValueError(f"the photo list's entry {handle!r} has a photo that is not [source, sha]: {ref!r}")
        out[handle] = [tuple(ref) for ref in refs]
    return out


def rows(sheet_text, photos):
    """(row, format, [(source, sha)] of the product's own photos in its order) per product row, the photos from the
    photo list (parse_photos). Fails loud on an unknown category (a guessed format would put a tile on a slab's stand)
    and on a product the photo list does not have (its photos are never read from the sheet's image columns)."""
    formats = {c.pcat_id: c.name for c in settings.active_categories()}     # the live registry: refreshed from the export
    for row in csv.DictReader(io.StringIO(sheet_text)):
        category = row["Product Category 1"]
        fmt = formats.get(category)
        if fmt is None:
            raise ValueError(f"unknown product category {category!r}: not an active category of this environment")
        handle = row["Product Handle"]
        if handle not in photos:
            raise ValueError(f"listing {handle!r} is not in the photo list: the sheet and its photo list differ")
        yield row, fmt, photos[handle]


def build(sheet_text, photos):
    """{sha: {"source", "key", "format", "size"}} from the product sheet's text and its photo list."""
    found = {}
    for row, fmt, shown in rows(sheet_text, photos):
        for src, sha in shown:
            if sha not in found:
                found[sha] = {"source": src, "key": row["Product Handle"], "format": fmt, "size": _size(row)}
    return found


def listings(sheet_text, photos):
    """[{"key", "source", "format", "photos"}] in sheet order: each product with its own photos (shas) in its order, the
    order a person sees them in, so the first good one is the one a listing is shown with."""
    return [{"key": row["Product Handle"], "source": shown[0][0], "format": fmt, "photos": [sha for _, sha in shown]}
            for row, fmt, shown in rows(sheet_text, photos) if shown]
