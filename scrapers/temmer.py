"""Temmer Marble scraper, migrated onto ScraperBase.

Temmer's WordPress install locks the 'product' custom post type out of the REST
API (404), so we scrape the publicly browsable HTML instead. Two site-specific
stages live here; folders, image naming, CSV, logging, retries, and the format
column are inherited from ScraperBase.

  STAGE 1 (list_products): walk every category index page, paginating
  /product-category/<slug>/page/N/ until a 404 or a page yields no new product
  links. Collect each unique product permalink and the categories it appeared
  in (a product can show up in several categories).

  STAGE 2 (parse_product): fetch each permalink and regex out title, slug, meta
  description, the product images (first = main slab photo, rest = project
  photos), the colour taxonomy terms, and the Turkish language alternate.

FORMAT (item 3): Temmer has no slab/block/tile field in the markup, and the
catalog is a mixed natural-stone offering with no per-product discriminator to
derive one from. The source's own language is slab-centric ("the FIRST image
flagged as the main slab photo", "main slab photo"), so the format is the
constant `slab`. NOTE: the colour menu on these pages is a junk global navigation
menu and there is no finish/quality data on the site -- we capture the colours
that link to /product-color/ as they appear, and do not invent finish/quality.

No JSON-LD or structured data is available, so the parser depends on stable
WordPress markup. If Temmer redesigns their theme this scraper needs updating.

Run:  python scrapers/temmer.py
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/temmer.py` and `python -m scrapers.temmer`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

BASE = "https://temmermarble.com"

# Every category we scrape (slug, display name). The trailing count from the
# probe is informational only -- we walk by slug until pages run out.
CATEGORIES = [
    ("natural-stone", "Natural Stone"),
    ("antique-collection", "Antique Collection"),
    ("marble", "Marble"),
    ("mesh-mounted-mosaic", "Mesh Mounted Mosaic"),
    ("granite", "Granite"),
    ("travertine", "Travertine"),
    ("onyx", "Onyx"),
    ("limestone", "Limestone"),
    ("honed-filled", "Honed & Filled"),
    ("sink", "Sink"),
    ("french-pattern-tumbled-travertine-and-marble", "French Pattern Tumbled"),
    ("stone-surface-finishings", "Stone Surface Finishings"),
    ("wall-stone", "Wall Stone"),
    ("profil-moulding", "Profil & Moulding"),
    ("pool-coping", "Pool Coping"),
    ("pebble-exterior-decorative-stone", "Pebble, Exterior Decorative Stone"),
]

MAX_INDEX_PAGES_PER_CATEGORY = 30

PRODUCT_LINK_RE = re.compile(
    r'<a[^>]+href=["\'](https?://temmermarble\.com/product/[^"\'/?#]+/?)["\']',
    re.I)

H1_RE = re.compile(r"<h1[^>]*>(.+?)</h1>", re.I | re.DOTALL)
TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)

# All <img> tags: capture src OR data-src/data-lazy-src (lazy loading)
IMG_RE = re.compile(
    r'<img\b[^>]*?(?:\bsrc|\bdata-src|\bdata-lazy-src)\s*=\s*["\']([^"\']+)["\']'
    r'[^>]*?(?:\balt\s*=\s*["\']([^"\']*)["\'])?',
    re.I)

# Color taxonomy links -> /product-color/<slug>/
COLOR_LINK_RE = re.compile(
    r'<a[^>]+href=["\']https?://temmermarble\.com/product-color/([\w-]+)/["\'][^>]*>([^<]+)</a>',
    re.I)

# Language alternate (Turkish slug)
TR_ALTERNATE_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]+hreflang=["\']tr["\'][^>]+href=["\']([^"\']+)["\']',
    re.I)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def is_product_image(src: str) -> bool:
    """Keep only the WordPress uploads images, filtering theme assets/icons."""
    if not src:
        return False
    if "wp-content/uploads" not in src:
        return False
    skip = ["/themes/", "/wp-includes/", "gravatar.com", "gtm.", "googletagmanager"]
    if any(s in src for s in skip):
        return False
    if src.lower().endswith(".svg"):
        return False
    return True


def extract_main_image(images: list[str], slug: str):
    """Decide which image is the main slab photo (first whose filename matches
    the slug, else the first overall). Returns (main_url, project_urls)."""
    if not images:
        return (None, [])

    slug_normalized = slug.lower().replace("-", "")
    main = None
    main_idx = None
    for i, src in enumerate(images):
        last = src.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        clean = re.sub(r"(?:-\d+x\d+|-1)+$", "", last)
        clean_normalized = clean.replace("-", "").replace("_", "")
        if (slug_normalized in clean_normalized
                or clean_normalized in slug_normalized
                or clean.startswith(slug)
                or slug in clean):
            main = src
            main_idx = i
            break

    if main is None:
        main = images[0]
        main_idx = 0

    projects = [im for i, im in enumerate(images) if i != main_idx]
    return (main, projects)


class TemmerScraper(ScraperBase):
    source = "temmer"
    category = "slab"      # constant: site has no slab/block/tile field; source
                           # language is slab-centric ("main slab photo")
    id_field = "slug"      # images named temmer_<slug>_<idx>
    columns = [
        "slug", "title", "permalink", "page_title", "description",
        "categories", "colors", "turkish_url",
        "image_main", "image_projects", "image_count_site", "image_count_projects",
    ]

    # -- STAGE 1: walk category indexes -------------------------------------
    def _collect_permalinks_for_category(self, cat_slug: str) -> list[str]:
        permalinks: list[str] = []
        seen: set[str] = set()
        for page_num in range(1, MAX_INDEX_PAGES_PER_CATEGORY + 1):
            if page_num == 1:
                url = f"{BASE}/product-category/{cat_slug}/"
            else:
                url = f"{BASE}/product-category/{cat_slug}/page/{page_num}/"

            self.log.info("  index page %d: %s", page_num, url)
            try:
                resp = self.get(url, allow_missing=True)
            except Exception as exc:
                # A real transient/persistent fetch failure is NOT end-of-pagination -- reading it as one
                # would silently drop the rest of the category and look like a clean run (products then read
                # as discontinuations). Mark the run INCOMPLETE so it is not ingested as authoritative, and
                # stop this category. Only a 404 (resp is None below) is a legitimate past-the-last-page stop.
                self.record_failure("http_index", category=cat_slug, page=page_num, url=url, error=str(exc))
                self.mark_incomplete(f"index fetch failed for {cat_slug} page {page_num}: {exc}")
                break
            if resp is None:
                # 404 -> WooCommerce returns this for a page past the last one: a genuine end-of-pagination.
                self.log.info("  no more pages after page %d (404)", page_num - 1)
                break

            page_hits = list(set(PRODUCT_LINK_RE.findall(resp.text)))
            new_count = 0
            for link in page_hits:
                link = link.rstrip("/") + "/"
                if link not in seen:
                    seen.add(link)
                    permalinks.append(link)
                    new_count += 1
            self.log.info("  -> %d products (%d new)", len(page_hits), new_count)

            if new_count == 0 and page_num > 1:
                self.log.info("  zero new products on page %d. Stopping.", page_num)
                break
        return permalinks

    def list_products(self) -> Iterable[Any]:
        # slug -> {permalink, categories: [...]}
        products: dict[str, dict] = {}
        for cat_slug, cat_name in CATEGORIES:
            self.log.info("Category: %s (%s)", cat_name, cat_slug)
            permalinks = self._collect_permalinks_for_category(cat_slug)
            self.log.info("  total in %s: %d products", cat_slug, len(permalinks))
            for permalink in permalinks:
                slug = permalink.rstrip("/").rsplit("/", 1)[-1]
                if slug not in products:
                    products[slug] = {"permalink": permalink, "categories": [cat_name]}
                elif cat_name not in products[slug]["categories"]:
                    products[slug]["categories"].append(cat_name)

        self.log.info("Unique products discovered: %d", len(products))
        for slug, info in products.items():
            yield {"slug": slug, **info}

    # -- STAGE 2: parse one product page ------------------------------------
    def parse_product(self, raw: dict) -> Optional[dict]:
        permalink = raw["permalink"]
        slug = raw["slug"]
        cats = raw.get("categories") or []

        try:
            html = self.get(permalink).text
        except Exception as exc:
            self.record_failure("product_page", slug=slug, url=permalink, error=str(exc))
            return None
        if not html:
            self.record_failure("product_page", slug=slug, url=permalink,
                                error="empty page")
            return None

        # Title (h1, falling back to og:title)
        h1_match = H1_RE.search(html)
        title = unescape(_strip_tags(h1_match.group(1))) if h1_match else ""
        if not title:
            og = OG_TITLE_RE.search(html)
            if og:
                title = unescape(og.group(1))

        page_title_match = TITLE_RE.search(html)
        page_title = unescape(page_title_match.group(1)).strip() if page_title_match else ""

        meta_desc_match = META_DESC_RE.search(html)
        description = unescape(meta_desc_match.group(1)).strip() if meta_desc_match else ""

        og_image_match = OG_IMAGE_RE.search(html)
        og_image = og_image_match.group(1) if og_image_match else ""

        # All product images, deduped, in order of appearance
        seen: set[str] = set()
        images: list[str] = []
        for src, _alt in IMG_RE.findall(html):
            if not is_product_image(src) or src in seen:
                continue
            seen.add(src)
            images.append(src)

        # Main image: trust og:image when it's a product image, else slug match,
        # else the first image overall.
        main_image = None
        if og_image and is_product_image(og_image):
            main_image = og_image
            if og_image not in seen:
                images.insert(0, og_image)
                seen.add(og_image)
        if main_image is None:
            main_image, _ = extract_main_image(images, slug)

        project_images = [im for im in images if im != main_image]

        # Colours (junk global menu on this site) - dedupe by slug, keep order
        color_seen: set[str] = set()
        colors: list[str] = []
        for color_slug, color_text in COLOR_LINK_RE.findall(html):
            if color_slug in color_seen:
                continue
            color_seen.add(color_slug)
            colors.append(unescape(color_text).strip())

        tr_match = TR_ALTERNATE_RE.search(html)
        tr_url = tr_match.group(1) if tr_match else ""

        # Full ordered list of source image URLs for the base (main first)
        image_urls = ([main_image] if main_image else []) + project_images

        return {
            "slug": slug,
            "title": title,
            "permalink": permalink,
            "page_title": page_title,
            "description": description,
            "categories": " | ".join(cats),
            "colors": " | ".join(colors),
            "turkish_url": tr_url,
            "image_main": main_image or "",
            "image_projects": " | ".join(project_images),
            "image_count_site": len(images),
            "image_count_projects": len(project_images),
            # base reads this (full source URLs, main first):
            "image_urls": image_urls,
        }


if __name__ == "__main__":
    TemmerScraper().run()
