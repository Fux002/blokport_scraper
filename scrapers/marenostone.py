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


# The real Length/Width/Thickness are CUSTOM product attributes rendered in the page's
# WooCommerce attributes table; the Store API exposes only the taxonomy (pa_*) attributes, so
# they must be read from the product-page HTML. Each row is:
#   <th ...__label">Length (cm)</th> <td ...__value"><p>140</p></td>
_DIM_ROW = re.compile(r'__label">\s*([^<]+?)\s*</th>\s*<td[^>]*__value">\s*<p>\s*([^<]*?)\s*</p>',
                      re.IGNORECASE | re.DOTALL)


def _dims_from_html(html: str) -> dict:
    """Length / Width / Thickness (with their unit from the label, e.g. '140cm') from a
    marenostone product page's attributes table. 'Length' is the long face, 'Width' the short
    face, 'Thickness' the depth."""
    out: dict = {}
    for label, value in _DIM_ROW.findall(html or ""):
        value = value.strip()
        if not value:
            continue
        unit = re.search(r"\((\w+)\)", label)
        valued = f"{value}{unit.group(1) if unit else 'cm'}"
        low = label.lower()
        if "length" in low:
            out["length"] = valued
        elif "width" in low:
            out["width"] = valued
        elif "thick" in low:
            out["thickness"] = valued
        elif "height" in low:
            out.setdefault("height", valued)
    return out


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

    def _page_dims(self, url: str) -> dict:
        """Fetch the product page and read its real Length/Width/Thickness from the attributes
        table (the Store API does not expose these custom attributes). Cached per permalink (the same
        product recurs across listing pages) so it is fetched at most once."""
        if not url:
            return {}
        cache = self.__dict__.setdefault("_dims_cache", {})
        if url in cache:
            return cache[url]
        try:
            dims = _dims_from_html(self.get(url).text)
        except Exception as exc:  # never let a missing page kill the row
            # record_failure (not just a warning) so silently-blank dimensions are AUDITABLE rather
            # than looking like a genuine source gap. Do NOT cache the failure: a later row sharing this
            # permalink can retry, instead of being permanently blanked by one transient miss.
            self.record_failure("dims", url=url, error=str(exc))
            self.log.warning("dimension fetch failed for %s: %s", url, exc)
            return {}
        cache[url] = dims                         # cache only successful fetches
        return dims

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
        # real dimensions live on the product page, not the Store API; 'Width' is the second face
        # dimension (-> our height), 'Thickness' is the depth (-> our width).
        pd = self._page_dims(p.get("permalink"))
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
            "dimensions_length": pd.get("length", ""),    # long face
            "dimensions_height": pd.get("width", ""),      # short face -> our height
            "dimensions_width": pd.get("thickness", ""),   # depth -> our width (thickness)
            "short_description": _clean(p.get("short_description", "")),
            "description": _clean(p.get("description", "")),
            "image_urls_thumb": " | ".join(thumbs),
            # base reads these:
            "image_urls": fulls,
            "format": _format(attrs.get("attr_format"), p.get("name", "")),  # item 3
        }


if __name__ == "__main__":
    MarenoStoneScraper().run()
