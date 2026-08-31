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
        Use self.get(url, ...) for fetching; it retries with backoff.

        PAGINATION: if your API pages by offset with NO total, do NOT hand-roll the stop condition -- a
        naive `break` on the first empty page treats a transient empty 200 as the end and silently delists
        the tail. Use the base paginator, which re-probes an empty offset once before accepting the end:

            def _fetch_page(self, offset): ...   # return the batch at offset (empty list at/after the end)
            def list_products(self):
                yield from self.paginate_offset(self._fetch_page, PAGE_SIZE)

        If your API DOES advertise a total (e.g. WooCommerce X-WP-TotalPages), page against that instead and
        call self.mark_incomplete(...) when an empty page arrives before the advertised end."""
        raise NotImplementedError

    def parse_product(self, raw: Any) -> Optional[dict]:
        """Return a row dict including:
          - the columns above,
          - `image_urls`: a list of full-size image urls (the base downloads them),
          - `format`: slab/block/tile when per-product (omit if `category` is set).
        Return None to skip the product.

        DETAIL FETCHES: if you fetch a per-product DETAIL page here, report EVERY outcome with
        self.note_detail(ok=True/False). The delist gate reads the resulting failure ratio and refuses to
        discontinue products against a badly rate-limited scrape (a dropped detail looks like a real
        absence). If the product's IDENTITY comes from the list page (the common case), a failed detail
        should KEEP the row and call self.mark_fetch_failed(row, "dims", ...) so the pipeline HOLDS it for
        retry instead of shipping defaulted values -- never return None on a recoverable detail failure, or
        the product silently drops from the scraped set and becomes a delist candidate."""
        return {
            "product_id": raw.get("id"),
            "name": raw.get("name"),
            "image_urls": raw.get("images", []),
            # "format": "slab",  # only if per-product
        }


if __name__ == "__main__":
    TemplateScraper().run()
