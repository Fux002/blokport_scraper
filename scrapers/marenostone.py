"""MarenoStone scraper, migrated onto ScraperBase (the proof of the framework).

Only the site-specific parts live here: list the products via the WooCommerce
Store API, and flatten one Woo product into a row. Folders, image download +
naming, CSV, logging, retries, and the format column are all inherited.

Run:  python scrapers/marenostone.py
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/marenostone.py` and `python -m scrapers.marenostone`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

API_URL = "https://marenostone.com/wp-json/wc/store/v1/products"
PER_PAGE = 100

# MarenoStone's six structured pa_* taxonomies -> our columns
ATTRIBUTE_NAMES = {
    "Product Category1": "attr_category1",
    "Product Category2": "attr_category2",
    "Product Format": "attr_format",   # slab / tile / block -> drives `format`
    "Surface Finish": "attr_finish",
    "Quality Grade": "attr_quality",
    "Priority": "attr_priority",
}


def _clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", (s or "")).strip()


def _format(attr_format: str, name: str) -> str:
    """Format from the source attribute; when it's missing or junk ('other', '')
    fall back to a slab/block/tile keyword in the product name (e.g. 'Cloud White
    Marble Slab' -> slab). Still-unresolved rows stay '' for the pipeline's
    format_resolve ladder to settle, rather than guessing here."""
    fmt = (attr_format or "").strip().lower()
    if fmt in ("slab", "block", "tile"):
        return fmt
    low = name.lower()
    for kw in ("slab", "block", "tile"):
        if kw in low:
            return kw
    return ""


def _attr(attributes: list, name: str) -> str:
    for a in attributes or []:
        if (a.get("name") or "").strip().lower() == name.lower():
            return " | ".join((t.get("name") or "").strip() for t in (a.get("terms") or []) if t)
    return ""


class MarenoStoneScraper(ScraperBase):
    source = "marenostone"
    category = None  # format is per-product (attr_format), not a constant
    columns = [
        "product_id", "name", "slug", "permalink", "sku",
        "attr_category1", "attr_category2", "attr_format", "attr_finish",
        "attr_quality", "attr_priority", "categories",
        "is_in_stock", "stock_status", "stock_text",
        "weight", "dimensions_length", "dimensions_width", "dimensions_height",
        "short_description", "description", "image_urls_thumb",
    ]

    def list_products(self) -> Iterable[Any]:
        page = 1
        while True:
            r = self.get(API_URL, params={"page": page, "per_page": PER_PAGE})
            batch = r.json()
            if not batch:
                break
            self.log.info("fetched page %d: %d products", page, len(batch))
            yield from batch
            if len(batch) < PER_PAGE:
                break
            page += 1

    def parse_product(self, p: dict) -> Optional[dict]:
        attributes = p.get("attributes") or []
        attrs = {col: _attr(attributes, disp) for disp, col in ATTRIBUTE_NAMES.items()}
        images = p.get("images") or []
        fulls, thumbs, seen = [], [], set()
        for img in images:
            src = (img.get("src") or "").strip()
            if src and src not in seen:
                seen.add(src)
                fulls.append(src)
                thumbs.append((img.get("thumbnail") or src).strip())
        dims = p.get("dimensions") or {}
        stock = p.get("stock_availability") or {}
        return {
            "product_id": p.get("id"),
            "name": _clean(p.get("name", "")),
            "slug": p.get("slug"),
            "permalink": p.get("permalink"),
            "sku": p.get("sku", ""),
            **attrs,
            "categories": " | ".join((c.get("name") or "") for c in (p.get("categories") or []) if c.get("name")),
            "is_in_stock": p.get("is_in_stock"),
            "stock_status": stock.get("class", ""),
            "stock_text": _clean(stock.get("text", "")),
            "weight": p.get("weight") or "",
            "dimensions_length": dims.get("length", ""),
            "dimensions_width": dims.get("width", ""),
            "dimensions_height": dims.get("height", ""),
            "short_description": _clean(p.get("short_description", "")),
            "description": _clean(p.get("description", "")),
            "image_urls_thumb": " | ".join(thumbs),
            # base reads these:
            "image_urls": fulls,
            "format": _format(attrs.get("attr_format"), p.get("name", "")),  # item 3
        }


if __name__ == "__main__":
    MarenoStoneScraper().run()
