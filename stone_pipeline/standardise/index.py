"""Which product a scraped photo belongs to: the GPU job works per photo and has no row, so it reads the emitted product
sheet. A photo used by several products takes the first in sheet order, so the choice is stable across runs."""
import csv
import io
import re

from stone_pipeline.config import settings

_URL = re.compile(r"/products/[a-z]+/([a-z_]+)/([0-9a-f]{64})\.")


def _size(row):
    try:
        length, width = float(row["Product Length"]), float(row["Product Width"])
    except (KeyError, TypeError, ValueError):
        return None
    return (length, width) if length > 0 and width > 0 else None


def _rows(sheet_text):
    """(row, format, [(source, sha)] in the row's image order, each once) per product row. Fails loud on an unknown
    category: a guessed format would put a tile on a slab's stand."""
    formats = {c.pcat_id: c.name for c in settings.active_categories()}     # the live registry: refreshed from the export
    for row in csv.DictReader(io.StringIO(sheet_text)):
        category = row["Product Category 1"]
        fmt = formats.get(category)
        if fmt is None:
            raise ValueError(f"unknown product category {category!r}: not an active category of this environment")
        found = [_URL.search(value or "") for column, value in row.items() if "Image" in column]
        yield row, fmt, list(dict.fromkeys((m.group(1), m.group(2)) for m in found if m))


def build(sheet_text):
    """{sha: {"source", "key", "format", "size"}} from the product sheet's text."""
    photos = {}
    for row, fmt, shown in _rows(sheet_text):
        for src, sha in shown:
            if sha not in photos:
                photos[sha] = {"source": src, "key": row["Product Handle"], "format": fmt, "size": _size(row)}
    return photos


def listings(sheet_text):
    """[{"key", "source", "format", "photos"}] in sheet order: each product with its photos (shas) in its own order,
    the order a person sees them in, so the first good one is the one a listing is shown with."""
    return [{"key": row["Product Handle"], "source": shown[0][0], "format": fmt, "photos": [sha for _, sha in shown]}
            for row, fmt, shown in _rows(sheet_text) if shown]
