"""Develi Marble (marbleslabs.tr) scraper, migrated onto ScraperBase.

The slabs gallery on https://www.marbleslabs.tr/slabs/ is rendered from a public
Google Sheets CSV: the page JS fetches the sheet's CSV export URL and renders one
card per row. So the only site-specific work here is GET the CSV, parse it with
the proper csv module, and flatten one row. Folders, image handling, naming, the
output CSV, logging, retries, and the format column are inherited.

Develi sells slab inventory (the page is literally the "Slabs Gallery"; the `type`
column is the stone kind such as marble/granite, not slab/block/tile), so the
format is the constant `slab`.

Images come from Google Drive's CDN (https://lh3.googleusercontent.com/d/{id}).
We expose the `=s2000` hi-res variant (max 2000px, original if smaller) as the
source image URL; the bare original is kept as a column for fallback.

Run:  python scrapers/develi.py
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/develi.py` and `python -m scrapers.develi`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

# Hardcoded into the marbleslabs.tr/slabs page JS. Stable as long as the sheet is
# "published to web"; if it 404s, view-source on /slabs/ and grep "spreadsheets/d/e/".
DATA_URL = ("https://docs.google.com/spreadsheets/d/e/"
            "2PACX-1vSfdlWikbo8PGmf3hahdi6fa2d8KlifPQkOYU5POqkL9-daJxk63xll"
            "PAzmnLI6mDCDf8491z7QpeOO/pub?gid=0&single=true&output=csv")

# Google Drive CDN: append "=s2000" for max 2000px (returns original if smaller).
IMAGE_SIZE_SUFFIX = "=s2000"

# Order matches the published sheet's columns. The page JS only uses the first 14
# (it doesn't know about gallery_url), but we capture all 15.
SOURCE_COLUMNS = ['bundle_code', 'name', 'type', 'color', 'surface',
                  'thickness_cm', 'height_cm', 'width_cm',
                  'slab_m2', 'slab_count', 'total_m2', 'weight_kg',
                  'stock_status', 'image_url', 'gallery_url']


def _add_size_suffix(url: str, suffix: str) -> str:
    """Append/replace the Drive CDN size suffix (=s2000) on a googleusercontent URL."""
    if not url or not suffix:
        return url
    m = re.search(r'(=s\d+(?:-c)?)$', url)
    if m:
        return url[:m.start()] + suffix
    return url + suffix


class DeveliScraper(ScraperBase):
    source = "develi"
    category = "slab"        # the /slabs/ gallery is slab inventory (item 3)
    id_field = "bundle_code"  # images named develi_<bundle_code>_<idx>
    columns = SOURCE_COLUMNS + ['image_url_hires']

    def list_products(self) -> Iterable[Any]:
        text = self.get(DATA_URL).text
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            self.log.warning("CSV had zero rows")
            return
        header = rows[0]
        self.log.info("source CSV header: %s", header)
        if len(header) < len(SOURCE_COLUMNS):
            self.log.warning("source CSV has %d columns, expected at least %d",
                             len(header), len(SOURCE_COLUMNS))
        count = 0
        for row in rows[1:]:
            if len(row) < len(SOURCE_COLUMNS):
                row = row + [''] * (len(SOURCE_COLUMNS) - len(row))
            item = {SOURCE_COLUMNS[i]: (row[i].strip() if row[i] else '')
                    for i in range(len(SOURCE_COLUMNS))}
            # Skip blank rows: no code AND no image
            if not item['bundle_code'] and not item['image_url']:
                continue
            count += 1
            yield item
        self.log.info("parsed %d slab rows from CSV", count)

    def parse_product(self, item: dict) -> Optional[dict]:
        url_original = (item.get('image_url') or '').strip()
        url_hires = _add_size_suffix(url_original, IMAGE_SIZE_SUFFIX)
        row = {col: item.get(col, '') for col in SOURCE_COLUMNS}
        row['image_url_hires'] = url_hires
        # base keeps these as source links (hi-res preferred):
        row['image_urls'] = [url_hires] if url_hires else []
        return row


if __name__ == "__main__":
    DeveliScraper().run()
