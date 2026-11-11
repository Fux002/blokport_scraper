"""Which product a scraped photo belongs to: the GPU job works per photo and has no row, so it reads the emitted product
sheet (each product's handle, category and size) and, beside it, the photo list (each product's own photos in its order:
the sheet's image columns carry only the chosen photo's views once it is published). A photo used by several products
takes the first in sheet order, so the choice is stable across runs."""
import csv
import io
import json
import re
from typing import NamedTuple

from stone_pipeline.config import categories
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


def parse_defaulted(text):
    """frozenset of the handles the defaulted-sizes list names (STANDARDISE.DEFAULTED_FILE: a sorted JSON list of distinct,
    non-empty handles, as the produce writes it). ValueError on anything else: read wrong, a guessed size would be drawn."""
    value = json.loads(text)                          # JSONDecodeError is a ValueError
    if not (isinstance(value, list) and all(isinstance(h, str) and h for h in value)):
        raise ValueError("the defaulted-sizes list is not a list of handles")
    if value != sorted(set(value)):
        raise ValueError("the defaulted-sizes list is not sorted and distinct, as the produce writes it")
    return frozenset(value)


def rows(sheet_text, photos):
    """(row, format, [(source, sha)] of the product's own photos in its order) per product row, the photos from the
    photo list (parse_photos). Fails loud on an unknown category (a guessed format would put a tile on a slab's stand)
    and on a product the photo list does not have (its photos are never read from the sheet's image columns)."""
    formats = {c.pcat_id: c.name for c in categories.active_categories()}     # the live registry: refreshed from the export
    for row in csv.DictReader(io.StringIO(sheet_text)):
        category = row["Product Category 1"]
        fmt = formats.get(category)
        if fmt is None:
            raise ValueError(f"unknown product category {category!r}: not an active category of this environment")
        handle = row["Product Handle"]
        if handle not in photos:
            raise ValueError(f"listing {handle!r} is not in the photo list: the sheet and its photo list differ")
        yield row, fmt, photos[handle]


def build(sheet_text, photos, defaulted):
    """{sha: {"source", "key", "format", "size"}} from the product sheet's text, its photo list and the handles whose size
    was defaulted (parse_defaulted): theirs reads None, unknown, as the sheet's numbers are the pack's guess."""
    found = {}
    for row, fmt, shown in rows(sheet_text, photos):
        handle = row["Product Handle"]
        size = None if handle in defaulted else _size(row)
        for src, sha in shown:
            if sha not in found:
                found[sha] = {"source": src, "key": handle, "format": fmt, "size": size}
    return found


def listings(sheet_text, photos):
    """[{"key", "sku", "source", "stone", "format", "photos"}] in sheet order: each product with its own photos (shas) in
    its order, the order a person sees them in, so the first good one is the one a listing is shown with. The key is the
    handle (it moves when a variety decision re-binds the product); the SKU never moves. stone = (variety id, finish id):
    what a photo of the listing shows."""
    return [{"key": row["Product Handle"], "sku": row["Variant Sku"], "source": shown[0][0],
             "stone": (row["STN Variation Id"], row["STN Finish Id"]), "format": fmt, "photos": [sha for _, sha in shown]}
            for row, fmt, shown in rows(sheet_text, photos) if shown]


class Shared(NamedTuple):
    """A photo several listings show: "siblings" when they are all of one stone (slabs of one block or bundle, tile
    sizes: the vendor's own practice, one decision is good for them all), "conflict" when they span varieties or finishes
    (a photo shows one stone: it is never chosen by itself for any of them, only the owner assigns it, to one listing)."""
    kind: str
    listings: tuple


def shared_photos(listings):
    """{(source, sha): Shared} of every photo more than one listing of a source shows; the one place a shared photo is
    classified. A listing's whole photo list counts: a photo belongs to the stone it shows, whichever slot it is in."""
    by_photo = {}
    for l in listings:
        for sha in l["photos"]:
            by_photo.setdefault((l["source"], sha), []).append(l)
    return {photo: Shared("siblings" if len({l["stone"] for l in shown}) == 1 else "conflict",
                          tuple(sorted(l["key"] for l in shown)))
            for photo, shown in sorted(by_photo.items()) if len(shown) > 1}


def groups(listings, conflicts):
    """{(source, sha): Shared} the owner's decisions and their page work with: the produce's stored conflict set
    (conflicts = {(source, sha): handles}, the one set every side sees), and for every other photo the sheet's listings
    share, its sibling group."""
    out = {photo: shared for photo, shared in shared_photos(listings).items()
           if shared.kind == "siblings" and photo not in conflicts}
    out.update({photo: Shared("conflict", tuple(keys)) for photo, keys in conflicts.items()})
    return out
