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


def build(sheet_text):
    """{sha: {"source", "key", "format", "size"}} from the product sheet's text. Fails loud on an unknown category:
    a guessed format would put a tile on a slab's stand."""
    formats = {c.pcat_id: c.name for c in settings.CATEGORIES if c.pcat_id}
    photos = {}
    for row in csv.DictReader(io.StringIO(sheet_text)):
        category = row["Product Category 1"]
        fmt = formats.get(category)
        if fmt is None:
            raise ValueError(f"unknown product category {category!r}: not an active category of this environment")
        for column, value in row.items():
            found = _URL.search(value or "") if "Image" in column else None
            if found and found.group(2) not in photos:
                photos[found.group(2)] = {"source": found.group(1), "key": row["Product Handle"], "format": fmt, "size": _size(row)}
    return photos
