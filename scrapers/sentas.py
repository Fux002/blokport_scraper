"""Sentas Marble (sentasmarble.com) scraper, on ScraperBase.

A plain PHP + jQuery site (no bot protection, no proxy). The slab inventory is organised per VARIETY:
/en/stocks lists every variety (name + /en/slabs/<slug> page); the variety page carries its grid id
(`kategoriID`) in an inline script, and POST /en/filter_data with that id returns the variety's bundle
cards (id, caption, product-page url). Every bundle fact lives on the product page: height/width (cm),
total sqm, total kg, piece count, surface finish, thickness, the photo gallery, and for some bundles
links to Google Drive folders with extra photos ("Drive Link" = bundle photos, "Scanner Photo" = one scan
per slab + a packing PDF). Those folder listings are read via Drive's public embedded folder view and the
photos appended after the site gallery, up to the pipeline's product-image slot count.

Stone type: the site publishes it only through its three collection grids (Marble / Travertine / Onyx),
keyed by variety name. A variety in none of them ships untyped and the pipeline types it from the matched
variety or holds it for review, never from a guess here.

The product page also renders an unlabelled vendor code ("( BH-12 )") in a leftover review-stars slot. It
is not a published grade and has no legend on the site, so it is deliberately not captured.

Run:  python -m scrapers.sentas
"""

from __future__ import annotations

import html
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
FILTER_URL = f"{SITE}/en/filter_data"
# The site's own type classification: collection grid id -> stone type (verified on the live collections).
COLLECTIONS = {"1": "Marble", "2": "Travertine", "3": "Onyx"}
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
# A public Drive folder's embedded listing (no API key) and its CDN image url; =s2000 caps the long side
# at 2000px (the original when smaller), the same form the develi scraper uses.
DRIVE_FOLDER_VIEW = "https://drive.google.com/embeddedfolderview?id={id}"
DRIVE_IMAGE = "https://lh3.googleusercontent.com/d/{id}=s2000"
_DRIVE_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
# Photos per bundle are capped at what the catalog can slot: a scanner folder holds one scan per slab (often
# 30+), and every url past the cap would be downloaded, enhanced and hosted for nothing.
MAX_IMAGES = SETTINGS.images.product_image_slots

_STOCK_VARIETY = re.compile(r'href="(https://sentasmarble\.com/en/slabs/[^"]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]+?)\s*<')
_KATEGORI = re.compile(r"kategoriID\s*=\s*'(\d+)'")
_GRID_ITEM = re.compile(r'data-id="(\d+)"\s+data-caption="([^"]*)".*?href="(https://sentasmarble\.com/en/product/[^"]+)"',
                        re.DOTALL)
_CAPTION = re.compile(r'data-caption="([^"]*)"')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
# "<strong>Height:</strong> ... <span class="pill-cm">165 cm</span>"
_DIM = re.compile(r"<strong>(Height|Width):</strong>.*?<span class=\"pill-cm\">([^<]*)</span>", re.DOTALL)
# The three figure tiles are identified by their icon, the only stable marker in that block.
_FIGURE = re.compile(r"icon_(sqm|weight|pieces)\.gif.*?<p[^>]*>\s*([\d.,]+)", re.DOTALL)
_SPEC = re.compile(r"<li><span>(Surface Finish|Thickness):</span>\s*([^<]*?)\s*</li>", re.DOTALL)
_GALLERY = re.compile(r'<ul id="smallGallery".*?</ul>', re.DOTALL)
_GALLERY_IMAGE = re.compile(r'data-image="([^"]+)"')
_DRIVE_FOLDER = re.compile(r"https://drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)")
_DRIVE_ENTRY = re.compile(r'<div class="flip-entry" id="entry-([^"]+)".*?<div class="flip-entry-title">([^<]*)</div>',
                          re.DOTALL)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _number(text: str) -> str:
    """'3,088.00' -> '3088.00': the site renders thousands separators, downstream parsers want a plain number."""
    return text.replace(",", "").strip()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _parse_product_page(page: str) -> dict:
    """The bundle facts a product page renders. Absent elements come back empty, never guessed."""
    dims = {k.lower(): v.strip() for k, v in _DIM.findall(page)}
    figures = {k: _number(v) for k, v in _FIGURE.findall(page)}
    specs = {k: v for k, v in _SPEC.findall(page)}
    gallery = _GALLERY.search(page)
    images = list(dict.fromkeys(_GALLERY_IMAGE.findall(gallery.group(0)))) if gallery else []
    h1 = _H1.search(page)
    return {
        "name": _text(h1.group(1)) if h1 else "",
        "height_cm": dims.get("height", ""),            # the short face
        "width_cm": dims.get("width", ""),              # the long face
        "total_sqm": figures.get("sqm", ""),
        "total_kg": figures.get("weight", ""),
        "pieces": figures.get("pieces", ""),
        "finish": specs.get("Surface Finish", ""),
        "thickness": specs.get("Thickness", ""),
        "image_urls": [f"{SITE}/{path}" for path in images],
        "drive_folders": list(dict.fromkeys(_DRIVE_FOLDER.findall(page))),
    }


class SentasScraper(ScraperBase):
    source = "sentas"
    category = "slab"   # /en/stocks is the "Slabs Inventory"; the site sells no blocks or tiles
    # DECLARED source convention: the product page renders dimensions in centimetres ("165 cm"); the
    # value is captured with its unit, so the adapter passes it through unchanged.
    dimension_unit = "cm"
    # A plain PHP host with no rate limiting observed; ~900 requests per scrape stay well-spaced at this.
    request_delay = (0.3, 0.8)
    columns = [
        "product_id", "name", "caption", "variety", "variety_url", "stone_type", "url",
        "finish", "thickness", "height_cm", "width_cm", "total_sqm", "total_kg", "pieces",
        "drive_folders", "drive_image_count",
    ]

    # --- listing ---------------------------------------------------------------------------------------
    def _type_map(self) -> dict[str, str]:
        """Normalised variety name -> stone type, from the site's three collection grids."""
        types: dict[str, str] = {}
        for grid_id, stone_type in COLLECTIONS.items():
            page = self.post(FILTER_URL, headers=AJAX_HEADERS, data={"kategoriID": grid_id, "sirala": "1"}).text
            for caption in _CAPTION.findall(page):
                types[_norm(html.unescape(caption))] = stone_type
        self.log.info("collection grids typed %d varieties", len(types))
        return types

    def _varieties(self) -> list[tuple[str, str]]:
        """(name, variety page url) for every variety on /en/stocks, in page order, deduped by url."""
        page = self.get(STOCKS_URL).text
        seen: dict[str, str] = {}
        for url, name in _STOCK_VARIETY.findall(page):
            if name := html.unescape(name).strip():   # the image anchor of a card has no text; the title has
                seen.setdefault(url, name)
        return [(name, url) for url, name in seen.items()]

    def _bundles(self, variety_url: str) -> list[tuple[str, str, str]]:
        """(bundle id, caption, product url) for one variety: its page gives the grid id, the grid the cards."""
        found = _KATEGORI.search(self.get(variety_url).text)
        if not found:
            raise RuntimeError(f"no kategoriID on {variety_url}")
        grid = self.post(FILTER_URL, headers=AJAX_HEADERS,
                         data={"kategoriID": found.group(1), "sirala": "1"}).text
        return [(pid, html.unescape(caption).strip(), url) for pid, caption, url in _GRID_ITEM.findall(grid)]

    def list_products(self) -> Iterable[Any]:
        types = self._type_map()
        varieties = self._varieties()
        self.log.info("stocks page lists %d varieties", len(varieties))
        for name, variety_url in varieties:
            try:
                bundles = self._bundles(variety_url)
            except Exception as exc:
                # This variety's bundles are unknown, not absent: record the run as truncated so the
                # pipeline never delists them off this scrape.
                self.record_failure("variety", url=variety_url, error=str(exc))
                self.mark_incomplete(f"variety grid failed: {variety_url}")
                continue
            for pid, caption, url in bundles:
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
