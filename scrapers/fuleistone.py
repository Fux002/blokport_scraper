"""Fuleistone scraper (stone.fuleistone.com), a WooCommerce Store API site like marenostone.

Scope: the "Stone Slabs" category (id 148) -- the supplier's full slab catalogue, which INCLUDES the
"Live Inventory (Slabs)" lots (a real m2 in the product name) alongside made-to-order varieties. The whole
scope is one format, so `category = "slab"` is a constant (no per-row format inference).

Only the site-specific parts live here: page the Store API for the category and flatten one product. Folders,
image download + naming, CSV, logging, retries, and the format column are inherited from ScraperBase.

Run:  python scrapers/fuleistone.py
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/fuleistone.py` and `python -m scrapers.fuleistone`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

API_URL = "https://stone.fuleistone.com/wp-json/wc/store/v1/products"
SLABS_CATEGORY_ID = 148   # "Stone Slabs" (the full slab catalogue; includes the live-inventory lots)
PER_PAGE = 100

# The Store API exposes the stone facts as pa_* taxonomy attributes; these are the ones we consume.
ATTRIBUTE_NAMES = {
    "Material": "material",     # -> raw_type (may list >1; the adapter collapses)
    "Color": "color",           # -> raw_color (may list >1)
    "Finish": "finish",         # -> raw_finish (may list >1; includes non-finish tags e.g. 'Bookmatched')
    "Thickness": "thickness",   # e.g. '20mm' -- the depth, present even when the full Size is not
    "Size": "size",             # e.g. '3280mm x 1830mm x 20mm' OR a qualitative label ('Rough Slab Size')
    "Quantity": "quantity",     # live-inventory lots only: 'Njj.jjm², Kpcs' -- a REAL piece count (K)
}

# A real per-slab size renders as 'L mm x W mm x T mm'; a qualitative Size ('Rough Slab Size',
# 'Project Customized Size') carries no digits and is left blank so derive fills the pack default.
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm",
                      re.IGNORECASE)
# Live-inventory lots publish the available stock as a square-metre figure in the product NAME,
# e.g. 'Instock Slabs - Statuario Marble - 148.36m2'. Captured as ready stock; derive turns it into a count.
_STOCK_M2_RE = re.compile(r"([\d.]+)\s*m²", re.IGNORECASE)
# ... and a REAL piece count in the Quantity attribute, e.g. '380.594m², 83pcs' -> 83. Preferred over the
# area estimate: it is the actual number of slabs in the lot.
_PCS_RE = re.compile(r"(\d+)\s*pcs", re.IGNORECASE)


def _clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def _attr(attributes: list, display: str) -> str:
    """The taxonomy terms for one attribute, pipe-joined RAW (multi-value is common: a slab can carry two
    materials or a layout tag beside its finish). The adapter applies the source's collapse rule; the
    scraper only captures, never picks."""
    for a in attributes or []:
        if (a.get("name") or "").strip().lower() == display.lower():
            return " | ".join((t.get("name") or "").strip() for t in (a.get("terms") or []) if t)
    return ""


def _dims_from_size(size_raw: str) -> tuple[str, str, str]:
    """(length, height, thickness) in mm from the Size attribute, or ('','','') when it is qualitative.
    Length/height are the two faces (L x W in the source); thickness is the depth (the third value)."""
    m = _SIZE_RE.search(size_raw or "")
    if not m:
        return "", "", ""
    return m.group(1), m.group(2), m.group(3)


class FuleistoneScraper(ScraperBase):
    source = "fuleistone"
    category = "slab"            # the whole "Stone Slabs" scope is one format (no per-row inference)
    # stone.fuleistone.com is Cloudflare-fronted (server: cloudflare / cf-ray), like the other slabware
    # sources. A plain-httpx request works from a residential IP but Cloudflare can block the datacenter IP
    # the scheduled task runs on, so route through curl_cffi (Chrome-TLS impersonation) + the residential
    # proxy -- for BOTH the Store API and the ~thousands of image downloads. Same treatment as marenostone.
    use_curl_cffi = True
    proxy_capability = "cloudflare_residential"
    # DECLARED source convention: fuleistone renders sizes in millimetres ('3280mm x 1830mm x 20mm'). Locked
    # here so the extractor never guesses a unit; the adapter maps with unit="mm" and derive converts.
    dimension_unit = "mm"
    columns = [
        "product_id", "name", "slug", "permalink", "sku",
        "material", "color", "finish", "thickness", "size",
        "dimensions_length", "dimensions_height", "stock_m2", "slab_count",
        "categories", "is_in_stock", "stock_status",
        "short_description", "description", "image_urls_thumb",
    ]

    def list_products(self) -> Iterable[Any]:
        page = 1
        total_pages = None
        while True:
            r = self.get(API_URL, params={"category": SLABS_CATEGORY_ID, "page": page, "per_page": PER_PAGE})
            if total_pages is None:
                # the Store API advertises the page count in a header; use it so a mid-pagination empty/short
                # response is caught as a TRUNCATED fetch (mark_incomplete), never mistaken for a clean end
                # (which would delist the missing tail).
                total_pages = int(r.headers.get("X-WP-TotalPages") or 0)
            batch = r.json()
            if not batch:
                if total_pages and page <= total_pages:   # empty BEFORE the advertised end == truncation
                    self.mark_incomplete(f"empty page {page} before advertised {total_pages}")
                break
            self.log.info("fetched page %d: %d products", page, len(batch))
            yield from batch
            page += 1
            if total_pages:
                if page > total_pages:                    # fetched every advertised page -> clean end
                    break
            elif len(batch) < PER_PAGE:                   # no header advertised -> short-page end signal
                break

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
        name = _clean(p.get("name", ""))
        length, height, size_thickness = _dims_from_size(attrs.get("size"))
        stock = p.get("stock_availability") or {}
        m2 = _STOCK_M2_RE.search(name)
        pcs = _PCS_RE.search(attrs.get("quantity", ""))   # real piece count on live-inventory lots
        return {
            "product_id": p.get("id"),
            "name": name,
            "slug": p.get("slug"),
            "permalink": p.get("permalink"),
            "sku": p.get("sku", ""),           # always blank on fuleistone -> the adapter keys on product_id
            **attrs,
            # dimensions parsed from the Size attribute (bare mm numbers; the adapter's build_dims applies the
            # declared mm unit). Blank when Size is qualitative -> derive fills the pack default.
            "dimensions_length": length,
            "dimensions_height": height,
            # thickness: the parsed Size depth (carry the mm unit -- raw_thickness has no unit helper, so a
            # bare '20' would misparse as 20 m), else the standalone Thickness attribute (already '20mm').
            "thickness": f"{size_thickness}mm" if size_thickness else attrs.get("thickness", ""),
            "stock_m2": m2.group(1) if m2 else "",            # live-inventory total available area (m2)
            "slab_count": pcs.group(1) if pcs else "",        # real piece count (preferred over the area estimate)
            "categories": " | ".join((c.get("name") or "") for c in (p.get("categories") or []) if c.get("name")),
            "is_in_stock": p.get("is_in_stock"),
            "stock_status": (stock.get("class", "") or "").strip(),   # 'in-stock' -> derive stock fallback
            "short_description": _clean(p.get("short_description", "")),
            "description": _clean(p.get("description", "")),
            "image_urls_thumb": " | ".join(thumbs),
            # base reads these:
            "image_urls": fulls,
        }


if __name__ == "__main__":
    FuleistoneScraper().run()
