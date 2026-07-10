"""Tureks scraper, migrated onto ScraperBase.

Tureks is WordPress + WooCommerce. The 'slabs' parent category holds individual
physical slab products: each is identified by SKU and embeds the material name,
finish, and block/slab numbers in its product name (e.g.
"Diana Royal Polished Slab 3238-299"). The canonical material name also comes
from a parallel material sub-category ('diana-royal', 'calacatta-amber', ...).

Site-specific bits here are the single WC Store API GET (category-filtered,
per_page above the catalog count so it fits one call), the product-name regex,
and the category/image flattening. Folders, naming, CSV, retries, and the format
column are inherited.

Tureks is slab inventory (the 'slabs' category, names end in "... Slab N-M"), so
the format is the constant `slab`.

Run:  python scrapers/tureks.py
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/tureks.py` and `python -m scrapers.tureks`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

BASE = "https://www.tureks.com"
API_URL = f"{BASE}/wp-json/wc/store/v1/products"
SLABS_CATEGORY_ID = 23172  # 'slabs' parent category
PER_PAGE = 100             # well above the catalog count, fetches all in one call

# Parse the product name. Examples:
#   "Diana Royal Polished Slab 3238-299"
#   "Calacatta Amber Polished Slab 1234-56"
# Group 1: material name, Group 2: finish, Group 3: block, Group 4: slab number.
NAME_RE = re.compile(
    r'^(.+?)\s+(Polished|Honed|Brushed|Bookmatched|Leathered|Matte|'
    r'Tumbled|Antique|Sandblasted|Sawn)\s+Slab\s+(\d+)[-_](\d+)\s*$',
    re.I)


def clean_html(s: str) -> str:
    """Decode HTML entities like &#8211; -> en dash. Returns plain string."""
    return unescape(s or "")


def parse_name(name: str):
    """Decompose 'Diana Royal Polished Slab 3238-299' into its parts.

    Returns (material, finish, block, slab_no). Falls back to ('', '', '', '')
    if the pattern doesn't match - the full name is preserved in 'name' anyway.
    """
    if not name:
        return ("", "", "", "")
    m = NAME_RE.match(name.strip())
    if m:
        return (m.group(1).strip(), m.group(2).strip(),
                m.group(3).strip(), m.group(4).strip())
    return ("", "", "", "")


def parse_categories(categories: list):
    """Tureks puts each slab in TWO categories: 'slabs' (the parent) and a
    material sub-category like 'diana-royal'. Returns
    (material_name, material_slug, pipe-joined names).
    """
    if not categories:
        return ("", "", "")
    material_name = ""
    material_slug = ""
    names = []
    for c in categories:
        slug = c.get("slug", "")
        name = c.get("name", "")
        names.append(name)
        if slug != "slabs" and not material_slug:
            material_slug = slug
            material_name = name
    return (material_name, material_slug, " | ".join(names))


class TureksScraper(ScraperBase):
    source = "tureks"
    category = "slab"        # Tureks is slab inventory (item 3: explicit format)
    id_field = "product_id"  # images named tureks_<product_id>_<idx>
    columns = [
        # Identity
        "product_id", "name", "slug", "permalink", "sku",
        # Material spec parsed from name + categories
        "material", "material_slug", "finish", "block", "slab_no",
        # Categories
        "categories",
        # Stock
        "is_in_stock", "is_on_backorder", "is_purchasable",
        "stock_status", "stock_text", "low_stock_remaining",
        # Pricing (zero across the catalog but kept for future visibility flips)
        "on_sale", "price", "regular_price", "sale_price",
        "currency", "price_html",
        # Physical
        "weight", "formatted_weight",
        "dimensions_length", "dimensions_width", "dimensions_height",
        "formatted_dimensions",
        # Images (full/thumb source links; image_count/local handled by base)
        "image_urls_full", "image_urls_thumb",
    ]

    def list_products(self) -> Iterable[Any]:
        """Single call with per_page=100 covers the catalog in one shot. The base
        warns via the format/parse paths; here we surface pagination overflow."""
        r = self.get(API_URL, params={
            "category": SLABS_CATEGORY_ID,
            "per_page": PER_PAGE,
        }, headers={"accept": "application/json"})
        total = r.headers.get("x-wp-total")
        total_pages = r.headers.get("x-wp-totalpages")
        products = r.json() or []
        self.log.info("tureks: got %d products (X-WP-Total=%s, X-WP-TotalPages=%s)",
                      len(products), total, total_pages)
        if total_pages and int(total_pages) > 1:
            # We fetched only page 1. Marking the run INCOMPLETE stops the pipeline from treating this
            # truncated scrape as the authoritative latest -- otherwise the missing rows 101+ would be
            # discontinued/stock-0 (a wrong delist). Fails loud: add pagination (page param) or raise
            # PER_PAGE, then the run is authoritative again.
            self.mark_incomplete(f"catalog has {total_pages} pages but only page 1 was fetched "
                                 f"(PER_PAGE={PER_PAGE}); add pagination before this scrape can delist")
        yield from products

    def parse_product(self, p: dict) -> Optional[dict]:
        name = clean_html(p.get("name", ""))
        material, finish, block, slab_no = parse_name(name)

        # Material name from the category sub-tag overrides name parsing if
        # present (more reliable for accents/casing than the name regex).
        material_cat_name, material_cat_slug, all_cats = parse_categories(
            p.get("categories") or [])
        if material_cat_name:
            material = material_cat_name

        # Tureks duplicates the featured image into the gallery, so dedupe by URL.
        images = p.get("images") or []
        fulls, thumbs, seen = [], [], set()
        for img in images:
            src = (img.get("src") or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)
            fulls.append(src)
            thumbs.append((img.get("thumbnail") or "").strip() or src)

        prices = p.get("prices") or {}
        stock_avail = p.get("stock_availability") or {}
        dims = p.get("dimensions") or {}

        return {
            # Identity
            "product_id":       p.get("id"),
            "name":             name,
            "slug":             p.get("slug"),
            "permalink":        p.get("permalink"),
            "sku":              p.get("sku", ""),
            # Material spec parsed from name + categories
            "material":         material,
            "material_slug":    material_cat_slug,
            "finish":           finish,
            "block":            block,
            "slab_no":          slab_no,
            # Categories
            "categories":       all_cats,
            # Stock
            "is_in_stock":      p.get("is_in_stock"),
            "is_on_backorder":  p.get("is_on_backorder"),
            "is_purchasable":   p.get("is_purchasable"),
            "stock_status":     stock_avail.get("class", ""),
            "stock_text":       clean_html(stock_avail.get("text", "")),
            "low_stock_remaining": p.get("low_stock_remaining"),
            # Pricing
            "on_sale":          p.get("on_sale"),
            "price":            prices.get("price", ""),
            "regular_price":    prices.get("regular_price", ""),
            "sale_price":       prices.get("sale_price", ""),
            "currency":         prices.get("currency_code", ""),
            "price_html":       clean_html(p.get("price_html", "")),
            # Physical
            "weight":           p.get("weight") or "",
            "formatted_weight": p.get("formatted_weight") or "",
            "dimensions_length": dims.get("length", ""),
            "dimensions_width":  dims.get("width", ""),
            "dimensions_height": dims.get("height", ""),
            "formatted_dimensions": p.get("formatted_dimensions") or "",
            # Images
            "image_urls_full":  " | ".join(fulls),
            "image_urls_thumb": " | ".join(thumbs),
            # base reads this (downloads/names + sets image_count/image_urls):
            "image_urls":       fulls,
        }


if __name__ == "__main__":
    TureksScraper().run()
