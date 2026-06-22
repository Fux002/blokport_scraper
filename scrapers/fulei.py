"""Fulei Stone scraper, migrated onto ScraperBase.

Only the site-specific parts live here: list the "Live Inventory (Slabs)"
products via the WooCommerce Store API (category id=246), and flatten one Woo
product into a row. Folders, image download + naming, CSV, logging, retries, and
the format column are all inherited.

Fulei's live-inventory category is slabs only, so the format is the constant
`slab` (the category is literally named "Live Inventory (Slabs)").

Prices come back as "0" with is_purchasable=False (Woo "contact for quote"); we
capture them anyway in case the supplier flips visibility for B2B buyers later.

The original monolith (scraper_fulei.py) is left untouched.

Run:  python scrapers/fulei.py
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/fulei.py` and `python -m scrapers.fulei`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

API_URL = "https://stone.fuleistone.com/wp-json/wc/store/v1/products"
LIVE_INVENTORY_CATEGORY_ID = 246  # "Live Inventory (Slabs)"
PER_PAGE = 100  # API max - 85 slabs comfortably fit in one call

KNOWN_ATTRS = {"NO.", "Material", "Color", "Thickness", "Finish", "Quantity", "Size"}
NOISE_CATEGORY_SLUGS = {"all", "p_02", "p_01", "p_03"}


def clean_html(s: str) -> str:
    """Decode HTML entities like &#8211; -> en dash. Returns plain string."""
    return unescape(s or "")


def get_attribute(attributes: list, attr_name: str) -> str:
    """Find an attribute by name (case-insensitive) and return joined term names.

    Stone slabs mix taxonomy attributes (pa_material, pa_color etc.) and free
    attributes (NO., Quantity, Size). Handle both uniformly.
    """
    if not attributes:
        return ""
    for attr in attributes:
        if (attr.get("name") or "").lower() == attr_name.lower():
            terms = attr.get("terms") or []
            return " | ".join((t.get("name") or "").strip() for t in terms if t)
    return ""


def parse_quantity(qty_str: str) -> tuple[str, str]:
    """Parse '256.054m², 43pcs' into (total_m2, slab_count).

    Handles '256.054m², 43pcs', '342.254 m2, 51 pcs', '104.797m²' (area only).
    """
    if not qty_str:
        return ("", "")
    qty_str = qty_str.strip()
    m2_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:m²|m2|sqm)", qty_str, re.I)
    pcs_match = re.search(r"(\d+)\s*pcs?", qty_str, re.I)
    return (
        m2_match.group(1) if m2_match else "",
        pcs_match.group(1) if pcs_match else "",
    )


def parse_size(size_str: str) -> str:
    """Return the raw slab size string (formats like '3200x1900x20mm' vary too
    much for safe structured parsing)."""
    return size_str.strip() if size_str else ""


def parse_categories(categories: list) -> str:
    """Pipe-join category names, excluding noise categories like 'All' / 'p_02'."""
    if not categories:
        return ""
    names = [c.get("name", "") for c in categories
             if (c.get("slug") or "").lower() not in NOISE_CATEGORY_SLUGS]
    return " | ".join(names)


class FuleiScraper(ScraperBase):
    source = "fulei"
    category = "slab"      # "Live Inventory (Slabs)" category: slab inventory (item 3)
    id_field = "product_id"  # images named fulei_<product_id>_<idx>
    columns = [
        # Identity
        "product_id", "name", "slug", "permalink", "sku", "supplier_code",
        # Material spec
        "material", "color", "thickness", "finish", "size",
        # Quantity / area
        "quantity_raw", "total_m2", "slab_count",
        # Other attributes
        "other_attributes", "categories",
        # Stock
        "is_in_stock", "is_on_backorder", "is_purchasable",
        "stock_status", "stock_text", "low_stock_remaining",
        # Pricing (empty/0 unless supplier flips visibility)
        "on_sale", "price", "regular_price", "sale_price",
        "currency", "price_html",
        # Physical
        "weight", "formatted_weight",
        "dimensions_length", "dimensions_width", "dimensions_height",
        "formatted_dimensions",
        # Images (full source URLs handled by base via image_urls; thumbs kept here)
        "image_urls_thumb",
    ]

    def list_products(self) -> Iterable[Any]:
        """Single GET filtered by category=246, per_page=100 (85 slabs fit in one
        call). Warn if X-WP-TotalPages says there's more than one page."""
        r = self.get(API_URL, params={
            "category": LIVE_INVENTORY_CATEGORY_ID,
            "per_page": PER_PAGE,
        }, headers={"accept": "application/json"})
        products = r.json()
        total = r.headers.get("x-wp-total")
        total_pages = r.headers.get("x-wp-totalpages")
        self.log.info("fulei: got %d products (X-WP-Total=%s, X-WP-TotalPages=%s)",
                      len(products), total, total_pages)
        if total_pages and int(total_pages) > 1:
            self.log.warning("catalog has more pages (X-WP-TotalPages=%s) than fetched; "
                             "increase PER_PAGE or add pagination.", total_pages)
        yield from products

    def parse_product(self, p: dict) -> Optional[dict]:
        attributes = p.get("attributes") or []

        supplier_code = get_attribute(attributes, "NO.")
        material = get_attribute(attributes, "Material")
        color = get_attribute(attributes, "Color")
        thickness = get_attribute(attributes, "Thickness")
        finish = get_attribute(attributes, "Finish")
        quantity_raw = get_attribute(attributes, "Quantity")
        size_raw = get_attribute(attributes, "Size")

        total_m2, slab_count = parse_quantity(quantity_raw)
        size = parse_size(size_raw)

        # Catch any other attributes we didn't explicitly map
        other_attrs = []
        for a in attributes:
            n = (a.get("name") or "").strip()
            if n and n not in KNOWN_ATTRS:
                terms = " | ".join((t.get("name") or "") for t in (a.get("terms") or []))
                if terms:
                    other_attrs.append(f"{n}={terms}")
        other_attributes = " ; ".join(other_attrs)

        # Images: full source URLs go to base (image_urls); thumbs kept as a column
        fulls, thumbs = [], []
        for img in (p.get("images") or []):
            src = (img.get("src") or "").strip()
            thumb = (img.get("thumbnail") or "").strip()
            if src:
                fulls.append(src)
                thumbs.append(thumb or src)

        prices = p.get("prices") or {}
        stock_avail = p.get("stock_availability") or {}
        dims = p.get("dimensions") or {}

        return {
            "product_id":       p.get("id"),
            "name":             clean_html(p.get("name", "")),
            "slug":             p.get("slug"),
            "permalink":        p.get("permalink"),
            "sku":              p.get("sku", ""),
            "supplier_code":    supplier_code,
            "material":         material,
            "color":            color,
            "thickness":        thickness,
            "finish":           finish,
            "size":             size,
            "quantity_raw":     quantity_raw,
            "total_m2":         total_m2,
            "slab_count":       slab_count,
            "other_attributes": other_attributes,
            "categories":       parse_categories(p.get("categories") or []),
            "is_in_stock":      p.get("is_in_stock"),
            "is_on_backorder":  p.get("is_on_backorder"),
            "is_purchasable":   p.get("is_purchasable"),
            "stock_status":     stock_avail.get("class", ""),
            "stock_text":       clean_html(stock_avail.get("text", "")),
            "low_stock_remaining": p.get("low_stock_remaining"),
            "on_sale":          p.get("on_sale"),
            "price":            prices.get("price", ""),
            "regular_price":    prices.get("regular_price", ""),
            "sale_price":       prices.get("sale_price", ""),
            "currency":         prices.get("currency_code", ""),
            "price_html":       clean_html(p.get("price_html", "")),
            "weight":           p.get("weight") or "",
            "formatted_weight": p.get("formatted_weight") or "",
            "dimensions_length": dims.get("length", ""),
            "dimensions_width":  dims.get("width", ""),
            "dimensions_height": dims.get("height", ""),
            "formatted_dimensions": p.get("formatted_dimensions") or "",
            "image_urls_thumb": " | ".join(thumbs),
            # base reads this (full source image URLs):
            "image_urls": fulls,
        }


if __name__ == "__main__":
    FuleiScraper().run()
