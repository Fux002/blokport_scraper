"""Sentas Marble (sentasmarble.com) scraper, on ScraperBase.

A Laravel site behind Cloudflare (plain requests pass; no proxy). The slab inventory is organised per
VARIETY: GET /en/stocks?ajax=1&page=N returns JSON pages of variety cards (name + /en/categories/<slug>);
GET /en/categories/<slug>?ajax=1&page=N returns JSON pages of that variety's bundle cards (product id,
caption, product-page url). Every bundle fact lives on the product page: height/width/thickness (cm),
total sqm, total kg, piece count, surface finish, colour, the photo gallery (the page's JSON-LD Product
image list), and for some bundles Google Drive folders with extra photos ("Google Drive" = bundle photos,
"Scanner Photo" = one scan per slab + a packing PDF). Those folder listings are read via Drive's public
embedded folder view and the photos appended after the site gallery, up to the pipeline's product-image
slot count.

Stone type: the site publishes it only through its three collection categories (marble / travertine /
onyx), keyed by variety name. A variety in none of them ships untyped and the pipeline types it from the
matched variety or holds it for review, never from a guess here.

Run:  python -m scrapers.sentas
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/sentas.py` and `python -m scrapers.sentas`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

from stone_pipeline.config.settings import SETTINGS

SITE = "https://sentasmarble.com"
STOCKS_URL = f"{SITE}/en/stocks"
CATEGORY_URL = f"{SITE}/en/categories/{{slug}}"
# The site's own type classification: collection category slug -> stone type (verified on the live site).
COLLECTIONS = {"marble-collection": "Marble", "travertine-collection": "Travertine", "onyx-collection": "Onyx"}
# Index cards that are GROUPINGS of bundles already listed under their variety, not varieties themselves.
INDEX_SKIP_SLUGS = {"new-slabs", "warehouse-special-products"}
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
# A public Drive folder's embedded listing (no API key) and its CDN image url; =s2000 caps the long side
# at 2000px (the original when smaller), the same form the develi scraper uses.
DRIVE_FOLDER_VIEW = "https://drive.google.com/embeddedfolderview?id={id}"
DRIVE_IMAGE = "https://lh3.googleusercontent.com/d/{id}=s2000"
_DRIVE_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
# Photos per bundle are capped at what the catalog can slot: a scanner folder holds one scan per slab (often
# 30+), and every url past the cap would be downloaded, enhanced and hosted for nothing.
MAX_IMAGES = SETTINGS.images.product_image_slots

_CATEGORY_CARD = re.compile(r'href="https://sentasmarble\.com/en/categories/([^"/]+)"[^>]*>\s*<img[^>]*alt="([^"]*)"')
_CARD_SPLIT = re.compile(r'<div class="tt-col-item">')
_CARD_ID = re.compile(r"openProductQuickView\('(\d+)'")
_CARD_LINK = re.compile(r'href="(https://sentasmarble\.com/en/products/[^"]+)"[^>]*>\s*<img[^>]*alt="([^"]*)"')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
# '<span class="dim-label">Height :</span> ... <span class="pill-cm">198 cm</span>'
_DIM = re.compile(r'<span class="dim-label">(Height|Width|Thickness)\s*:</span>.*?<span class="pill-cm">([^<]*)</span>',
                  re.DOTALL)
# The three figure tiles are identified by their icon, the only stable marker in that block.
_FIGURE = re.compile(r'icon_(sqm|weight|pieces)\.gif.*?<div[^>]*stat-main-val[^>]*>\s*([\d.,]+)', re.DOTALL)
_SPEC = re.compile(r'<td[^>]*spec-label[^>]*>(Surface Finish|Thickness|Colors):</td>\s*<td[^>]*>(.*?)</td>', re.DOTALL)
_JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
# The product page's Drive action cards: a folder link in either of Drive's url forms, labelled by the card.
_ACTION_CARD = re.compile(r'<a href="([^"]+)"[^>]*class="action-card-item[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
_DRIVE_FOLDER_ID = re.compile(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|open\?id=)([A-Za-z0-9_-]+)")
_DRIVE_ENTRY = re.compile(r'<div class="flip-entry" id="entry-([^"]+)".*?<div class="flip-entry-title">([^<]*)</div>',
                          re.DOTALL)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _number(text: str) -> str:
    """'3,088.00' -> '3088.00': the site renders thousands separators, downstream parsers want a plain number."""
    return text.replace(",", "").strip()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _gallery(page: str) -> list[str]:
    """The product's own photos: the JSON-LD Product node's image list (the gallery; the page also shows
    similar products, which the structured data does not include)."""
    for raw in _JSON_LD.findall(page):
        try:
            node = json.loads(raw)
        except ValueError:
            continue
        if node.get("@type") == "Product" and isinstance(node.get("image"), list):
            return list(dict.fromkeys(u for u in node["image"] if isinstance(u, str) and u))
    return []


def _drive_folders(page: str) -> list[str]:
    """Folder ids of the page's Drive photo cards, in page order (the packing-list sheet is not a folder)."""
    ids = []
    for href, body in _ACTION_CARD.findall(page):
        found = _DRIVE_FOLDER_ID.search(html.unescape(href))
        if found and "Drive Folder" in _text(body):
            ids.append(found.group(1))
    return list(dict.fromkeys(ids))


def _parse_product_page(page: str) -> dict:
    """The bundle facts a product page renders. Absent elements come back empty, never guessed."""
    dims = {k.lower(): v.strip() for k, v in _DIM.findall(page)}
    figures = {k: _number(v) for k, v in _FIGURE.findall(page)}
    specs = {k: _text(v) for k, v in _SPEC.findall(page)}
    h1 = _H1.search(page)
    return {
        "name": _text(h1.group(1)) if h1 else "",
        "height_cm": dims.get("height", ""),            # the short face
        "width_cm": dims.get("width", ""),              # the long face
        "thickness": specs.get("Thickness") or dims.get("thickness", ""),
        "total_sqm": figures.get("sqm", ""),
        "total_kg": figures.get("weight", ""),
        "pieces": figures.get("pieces", ""),
        "finish": specs.get("Surface Finish", ""),
        "color": specs.get("Colors", ""),
        "image_urls": _gallery(page),
        "drive_folders": _drive_folders(page),
    }


def _parse_cards(cards_html: str) -> list[tuple[str, str, str]]:
    """(product id, caption, product url) per bundle card of a category page."""
    out = []
    for card in _CARD_SPLIT.split(cards_html)[1:]:
        pid, link = _CARD_ID.search(card), _CARD_LINK.search(card)
        if pid and link:
            out.append((pid.group(1), html.unescape(link.group(2)).strip(), link.group(1)))
    return out


class SentasScraper(ScraperBase):
    source = "sentas"
    category = "slab"   # /en/stocks is the slab inventory; the site sells no blocks or tiles
    # DECLARED source convention: the product page renders dimensions in centimetres ("198 cm"); the
    # value is captured with its unit, so the adapter passes it through unchanged.
    dimension_unit = "cm"
    # No rate limiting observed; ~800 requests per scrape stay well-spaced at this.
    request_delay = (0.3, 0.8)
    columns = [
        "product_id", "name", "caption", "variety", "variety_url", "stone_type", "url",
        "finish", "color", "thickness", "height_cm", "width_cm", "total_sqm", "total_kg", "pieces",
        "drive_folders", "drive_image_count",
    ]

    # --- listing ---------------------------------------------------------------------------------------
    def _pages(self, url: str) -> Iterable[dict]:
        """Every JSON page of a paginated listing (?ajax=1&page=N), following the server's hasMore flag."""
        page = 1
        while True:
            data = self.get(url, params={"ajax": "1", "page": str(page)}, headers=AJAX_HEADERS).json()
            yield data
            if not data.get("hasMore"):
                return
            page += 1

    def _category_names(self, slug: str) -> list[str]:
        """The card titles of one collection category (variety names; its cards link to the variety's
        showcase page, the same card shape as a bundle card), across all its pages."""
        names = []
        for data in self._pages(CATEGORY_URL.format(slug=slug)):
            names.extend(html.unescape(n).strip() for _, n in _CARD_LINK.findall(data.get("cards_html") or ""))
        return names

    def _type_map(self) -> dict[str, str]:
        """Normalised variety name -> stone type, from the site's three collection categories."""
        types = {_norm(name): stone_type
                 for slug, stone_type in COLLECTIONS.items() for name in self._category_names(slug)}
        self.log.info("collection categories typed %d varieties", len(types))
        return types

    def _varieties(self) -> list[tuple[str, str]]:
        """(name, category url) for every variety on /en/stocks, in page order, deduped by slug."""
        seen: dict[str, str] = {}
        for data in self._pages(STOCKS_URL):
            for slug, name in _CATEGORY_CARD.findall(data.get("html") or ""):
                if slug not in INDEX_SKIP_SLUGS and (name := html.unescape(name).strip()):
                    seen.setdefault(slug, name)
        return [(name, CATEGORY_URL.format(slug=slug)) for slug, name in seen.items()]

    def _bundles(self, variety_url: str) -> list[tuple[str, str, str]]:
        """(product id, caption, product url) for one variety, across all its pages; the advertised total
        is checked so a short page is a truncation, not a quiet end."""
        bundles, total = [], None
        for data in self._pages(variety_url):
            total = data.get("total", total)
            bundles.extend(_parse_cards(data.get("cards_html") or ""))
        if total is not None and len(bundles) < int(total):
            raise RuntimeError(f"{variety_url}: {len(bundles)} cards for an advertised total of {total}")
        return bundles

    def list_products(self) -> Iterable[Any]:
        types = self._type_map()
        varieties = self._varieties()
        self.log.info("stocks index lists %d varieties", len(varieties))
        seen: set[str] = set()   # a bundle listed under two cards is one product; the first card names it
        for name, variety_url in varieties:
            try:
                bundles = self._bundles(variety_url)
            except Exception as exc:
                # This variety's bundles are unknown, not absent: record the run as truncated so the
                # pipeline never delists them off this scrape.
                self.record_failure("variety", url=variety_url, error=str(exc))
                self.mark_incomplete(f"variety listing failed: {variety_url}")
                continue
            for pid, caption, url in bundles:
                if pid in seen:
                    continue
                seen.add(pid)
                yield {"product_id": pid, "caption": caption, "url": url, "variety": name,
                       "variety_url": variety_url, "stone_type": types.get(_norm(name), "")}

    # --- detail ----------------------------------------------------------------------------------------
    def _drive_images(self, folder_id: str) -> list[str]:
        """CDN urls of the image files in a public Drive folder (PDF packing lists skipped). A folder that
        fails to list is recorded and contributes nothing: Drive photos are an extra, never a hold."""
        try:
            listing = self.get(DRIVE_FOLDER_VIEW.format(id=folder_id)).text
        except Exception as exc:
            self.record_failure("drive", id=folder_id, error=str(exc))
            return []
        return [DRIVE_IMAGE.format(id=file_id) for file_id, title in _DRIVE_ENTRY.findall(listing)
                if title.lower().endswith(_DRIVE_IMAGE_EXT)]

    def _images(self, detail: dict) -> tuple[list[str], int]:
        """Site gallery first, then Drive photos, capped at MAX_IMAGES. Returns (urls, drive photos used)."""
        urls = list(detail["image_urls"])
        for folder_id in detail["drive_folders"]:
            if len(urls) >= MAX_IMAGES:
                break
            urls.extend(self._drive_images(folder_id))
        urls = list(dict.fromkeys(urls))[:MAX_IMAGES]
        return urls, max(0, len(urls) - len(detail["image_urls"]))

    def parse_product(self, item: dict) -> Optional[dict]:
        try:
            detail = _parse_product_page(self.get(item["url"]).text)
        except Exception as exc:
            self.record_failure("detail", product_id=item["product_id"], url=item["url"], error=str(exc))
            self.note_detail(ok=False)   # feeds the delist gate's detail-failure ratio
            detail = None
        else:
            self.note_detail(ok=True)
        images, drive_count = self._images(detail) if detail else ([], 0)
        facts = detail or {}
        row = {
            **item,
            "name": facts.get("name", ""),
            "finish": facts.get("finish", ""),
            "color": facts.get("color", ""),
            "thickness": facts.get("thickness", ""),
            "height_cm": facts.get("height_cm", ""),
            "width_cm": facts.get("width_cm", ""),
            "total_sqm": facts.get("total_sqm", ""),
            "total_kg": facts.get("total_kg", ""),
            "pieces": facts.get("pieces", ""),
            "drive_folders": " | ".join(facts.get("drive_folders", [])),
            "drive_image_count": drive_count,
            "image_urls": images,   # base keeps these as source links
        }
        if detail is None:
            # size, finish, count and photos all live on the product page: HOLD the row for a retry next
            # scrape rather than let derive default a fabricated size (same as polonine / marenostone).
            self.mark_fetch_failed(row, "dims", product_id=item["product_id"], url=item["url"],
                                   error="product page fetch failed")
        return row


if __name__ == "__main__":
    SentasScraper().run()
