"""Template for a new site scraper. Copy to scrapers/<site>.py and fill in the
two methods. Everything else (folders, image download + naming, CSV, logging,
format column) is inherited from ScraperBase.

Run:  python scrapers/<site>.py
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

try:
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase


class TemplateScraper(ScraperBase):
    source = "CHANGE_ME"            # the source id (also the data/ folder name)
    category = "slab"              # constant format if the whole site is one kind,
                                   # else set `format` per product in parse_product
                                   # and leave this None. Must be slab/block/tile.
    columns = [                    # the site-specific CSV columns you produce
        "product_id", "name",      # (image/format/timestamp columns are added by the base)
    ]

    def list_products(self) -> Iterable[Any]:
        """Yield each raw product (a dict, a URL, whatever parse_product needs).
        Use self.get(url, ...) for fetching; it retries with backoff."""
        raise NotImplementedError

    def parse_product(self, raw: Any) -> Optional[dict]:
        """Return a row dict including:
          - the columns above,
          - `image_urls`: a list of full-size image urls (the base downloads them),
          - `format`: slab/block/tile when per-product (omit if `category` is set).
        Return None to skip the product."""
        return {
            "product_id": raw.get("id"),
            "name": raw.get("name"),
            "image_urls": raw.get("images", []),
            # "format": "slab",  # only if per-product
        }


if __name__ == "__main__":
    TemplateScraper().run()
