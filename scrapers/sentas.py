"""Sentas Marble (sentasmarble.com) scraper, on ScraperBase.

A Laravel site behind Cloudflare (plain requests pass; no proxy). The slab inventory is organised per
VARIETY: GET /en/stocks?ajax=1&page=N returns JSON pages of variety cards (name + /en/categories/<slug>);
GET /en/categories/<slug>?ajax=1&page=N returns JSON pages of that variety's bundle cards (product id,
caption, product-page url). Every bundle fact lives on the product page: height/width/thickness (cm),
total sqm, total kg, piece count, surface finish, colour, the photo gallery (the page's JSON-LD Product
image list), and for some bundles Google Drive folders with extra photos ("Google Drive" = bundle photos,
"Scanner Photo" = one scan per slab + the block sheet). Those folder listings are read via Drive's public
embedded folder view and the photos appended after the site gallery, up to the pipeline's product-image
slot count. The per-slab scans (custom_<block>_<slab>.jpg) are also written as the bundle's `piece_scans`:
each slab's number and scan, with the slab's own measured size and weight from the block sheet; the loader
decides which slabs the bundle is sold as (stages/lots.py).

Block sheet: the vendor's scanner cutting list (a PDF beside the scans): one entry per slab with its
measured length x width (cm), area and weight (kg). It is read only with certainty: the sheet's block must be
the scans' block and its slabs must add up to its own totals, else it is a problem that holds the bundle for
retry (never offered on the bundle's size instead). A void entry (no size, a stand-in scan) is not a slab.
Its thickness column is not used: it disagrees with the listed thickness on some sheets.

Packing list: where the page links one (a Google Sheet), it is the bundle's stock source. Only the vendor's
own summary rows are read (TOTAL and IN STOCK, reserved counted as in stock): a sheet can carry stray rows
copied from another block, and only the summary is this product's figure. Written as packing_available /
packing_sold with packing_status ok | failed | unreadable (empty when the bundle has none).

Stone type: the site publishes it only through its three collection categories (marble / travertine /
onyx), keyed by variety name. A variety in none of them ships untyped and the pipeline types it from the
matched variety or holds it for review, never from a guess here.

Run:  python -m scrapers.sentas
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

try:  # allow both `python scrapers/sentas.py` and `python -m scrapers.sentas`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
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
DRIVE_FILE = "https://drive.google.com/uc?export=download&id={id}"
_DRIVE_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
# Photos per bundle are capped at what the catalog can slot: a scanner folder holds one scan per slab (often
# 30+), and every url past the cap would be downloaded, enhanced and hosted for nothing.
MAX_IMAGES = SETTINGS.images.product_image_slots
# A Scanner Photo file: one scan per slab, named by block and slab number (the slab's place in the cut).
_SLAB_SCAN = re.compile(r"^custom_(\d+)_(\d+)\.(?:jpe?g|png|webp)$", re.IGNORECASE)
# The block sheet PDF to text, its page layout kept (poppler's pdftotext, installed in the image).
PDF_TO_TEXT = ("pdftotext", "-layout", "-enc", "UTF-8")
PDF_TO_TEXT_TIMEOUT_S = 60
# The sheet prints each slab's area to 0.001 m2 and weight to 0.1 kg: per slab, its rounding is all the
# slabs' sum may differ from the sheet's own header totals.
BLOCK_SHEET_M2_ROUNDING = 0.001
BLOCK_SHEET_KG_ROUNDING = 0.1
# The packing list: a Google Sheet linked from the product page, read as CSV (public export, no API key).
PACKING_LIST_CSV = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv"
_PACKING_SHEET = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")

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
# Block sheet lines: the header, a row of one or two slab numbers (from the left margin; page numbers are
# indented far right), then each slab's SIZE and FINS (area, weight; a trailing * marks a calculated weight).
_SHEET_BLOCK = re.compile(r"Block:\s*(\S+)")
_SHEET_TOTALS = re.compile(r"Weight:\s*([\d.]+)\s*Kg\s+Area:\s*([\d.]+)\s*m2")
_SHEET_LABELS = re.compile(r"^ ?(\d+)(?:\s{10,}(\d+))?\s*$")
_SHEET_SIZE = re.compile(r"SIZE\s+(-?[\d.]+)\s*x\s*(-?[\d.]+)\s*x\s*-?[\d.]+")
_SHEET_FINS = re.compile(r"FINS\s+.*?\s+A\s+(-?[\d.]+)\s*m2\s+W\s+(-?[\d.]+)\s*kg")


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


def _packing_list_id(page: str) -> str:
    """The Google Sheet id of the page's "Packing List" card, or '' (none, or a link that is not a Sheet)."""
    for href, body in _ACTION_CARD.findall(page):
        if "Packing List" in _text(body):
            found = _PACKING_SHEET.search(html.unescape(href))
            return found.group(1) if found else ""
    return ""


def _packing_counts(text: str) -> Optional[tuple[int, int]]:
    """(in stock, sold) from the packing list's own summary rows, or None when it has no readable summary.
    IN STOCK includes reserved slabs; sold = TOTAL - IN STOCK. The piece column is the first PCS/PIECES."""
    rows = list(csv.reader(io.StringIO(text)))
    header = next((r for r in rows if r and r[0].strip().upper().startswith("BLOCK")), None)
    if header is None:
        return None
    names = [c.strip().upper() for c in header]
    pcs = next((i for i, name in enumerate(names) if name in ("PCS", "PIECES")), None)
    summary: dict[str, list[str]] = {}
    for r in rows:
        label = r[0].strip().upper() if r else ""
        if label in ("TOTAL", "IN STOCK"):
            summary.setdefault(label, r)
    if pcs is None or len(summary) != 2:
        return None
    total, in_stock = (_count(summary[label][pcs] if len(summary[label]) > pcs else "") for label in ("TOTAL", "IN STOCK"))
    if total is None or in_stock is None or in_stock > total:
        return None
    return in_stock, total - in_stock


def _count(text: str) -> Optional[int]:
    value = _number(text)
    return int(value) if value.isdigit() else None


class BlockSheet(NamedTuple):
    block: str
    slabs: dict[int, tuple[float, float, float]]   # slab number -> (length cm, width cm, weight kg), measured only


def _block_sheet(text: str) -> Optional[BlockSheet]:
    """The measured slabs of a block sheet's text (pdftotext -layout), or None when it cannot be read with
    certainty: no block or totals header, a row of slab numbers without exactly its sizes and figures, a
    slab listed twice, or slabs that do not add up to the header totals. A void entry (no positive size) is
    read, counted in the totals, and left out: it is not a slab. The totals check is what makes a misread
    impossible to pass: any slab read wrongly or missed no longer adds up."""
    block, totals = _SHEET_BLOCK.search(text), _SHEET_TOTALS.search(text)
    if not block or not totals:
        return None
    entries: list[tuple[int, float, float, float, float]] = []   # (slab, length, width, area, weight)
    labels: list[int] = []
    sizes: list[tuple[float, float]] = []
    for line in text.splitlines():
        if _SHEET_BLOCK.search(line):
            # a page header: what was pending above it is the page title (on some sheets the bare block
            # number); a real slab row lost here can only show as totals that no longer add up
            labels, sizes = [], []
        elif found := _SHEET_LABELS.match(line):
            if labels:
                return None                          # the previous row's sizes never came
            labels = [int(n) for n in found.groups() if n]
        elif labels and (found_sizes := _SHEET_SIZE.findall(line)):
            if sizes or len(found_sizes) != len(labels):
                return None
            sizes = [(float(length), float(width)) for length, width in found_sizes]
        elif labels and (figures := _SHEET_FINS.findall(line)):
            if len(figures) != len(sizes):
                return None
            entries += [(n, length, width, float(area), float(kg))
                        for n, (length, width), (area, kg) in zip(labels, sizes, figures)]
            labels, sizes = [], []
    numbers = [entry[0] for entry in entries]
    if labels or not entries or len(set(numbers)) != len(numbers):
        return None
    total_kg, total_m2 = float(totals.group(1)), float(totals.group(2))
    if (abs(sum(entry[4] for entry in entries) - total_kg) > BLOCK_SHEET_KG_ROUNDING * len(entries)
            or abs(sum(entry[3] for entry in entries) - total_m2) > BLOCK_SHEET_M2_ROUNDING * len(entries)):
        return None
    return BlockSheet(block.group(1), {n: (length, width, kg) for n, length, width, _, kg in entries
                                       if length > 0 and width > 0 and kg > 0})


def _pdf_text(data: bytes) -> Optional[str]:
    """A PDF's text with its page layout kept, or None when it cannot be converted. A missing pdftotext is
    an environment fault, not a bad sheet: it raises."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sheet.pdf"
        path.write_bytes(data)
        try:
            done = subprocess.run([*PDF_TO_TEXT, str(path), "-"], capture_output=True, check=True,
                                  timeout=PDF_TO_TEXT_TIMEOUT_S)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
    return done.stdout.decode("utf-8", errors="replace")


def _photo_urls(gallery: list[str], files: list[tuple[str, str]]) -> tuple[list[str], int]:
    """(bundle photos, how many came from Drive): the site gallery first, then every Drive image, capped at
    MAX_IMAGES."""
    drive = [DRIVE_IMAGE.format(id=file_id) for title, file_id in files if title.lower().endswith(_DRIVE_IMAGE_EXT)]
    urls = list(dict.fromkeys([*gallery, *drive]))[:MAX_IMAGES]
    return urls, max(0, len(urls) - len(gallery))


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
        "packing_list": _packing_list_id(page),
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
        "drive_folders", "drive_image_count", "piece_scans",
        "packing_status", "packing_available", "packing_sold",
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
    def _drive_files(self, folder_id: str) -> Optional[list[tuple[str, str]]]:
        """(title, file id) of every file in a public Drive folder, or None when it fails to list (recorded):
        its contents, which may be the slab scans and the block sheet, are then unknown, not absent."""
        try:
            listing = self.get(DRIVE_FOLDER_VIEW.format(id=folder_id)).text
        except Exception as exc:
            self.record_failure("drive", id=folder_id, error=str(exc))
            return None
        return [(title.strip(), file_id) for file_id, title in _DRIVE_ENTRY.findall(listing)]

    def _pieces(self, folders: list[list[tuple[str, str]]]) -> tuple[list[dict], str]:
        """(the per-slab scans in cut order, a block-sheet problem or ''). Each scan: {"position", "image_urls"}
        plus, when the block sheet beside the scans measures that slab, "length_cm", "width_cm", "weight_kg".
        Scans with no sheet beside them are listed unmeasured. A sheet that cannot be used is a problem: no
        slab is measured from it, and the caller holds the bundle."""
        scan_folders = [files for files in folders if any(_SLAB_SCAN.match(title) for title, _ in files)]
        scans: dict[int, list[str]] = {}
        blocks: set[str] = set()
        for files in scan_folders:
            for title, file_id in files:
                if found := _SLAB_SCAN.match(title):
                    blocks.add(found.group(1))
                    scans.setdefault(int(found.group(2)), []).append(DRIVE_IMAGE.format(id=file_id))
        pieces = [{"position": slab, "image_urls": scans[slab]} for slab in sorted(scans)]
        sheets = [file_id for files in scan_folders for title, file_id in files if title.lower().endswith(".pdf")]
        if not sheets:
            return pieces, ""
        sheet, problem = self._block_sheet(sheets, blocks)
        if sheet is None:
            return pieces, problem
        for piece in pieces:
            if measured := sheet.slabs.get(piece["position"]):
                piece.update(zip(("length_cm", "width_cm", "weight_kg"), measured))
        return pieces, ""

    def _block_sheet(self, sheet_ids: list[str], blocks: set[str]) -> tuple[Optional[BlockSheet], str]:
        """The block sheet beside the scans, or (None, why it cannot be used)."""
        if len(sheet_ids) != 1:
            return None, f"{len(sheet_ids)} PDFs beside the scans, the block sheet is ambiguous"
        try:
            data = self.get(DRIVE_FILE.format(id=sheet_ids[0])).content
        except Exception as exc:
            return None, f"download failed: {exc}"
        text = _pdf_text(data) if data.startswith(b"%PDF") else None
        sheet = _block_sheet(text) if text is not None else None
        if sheet is None:
            return None, "unreadable (not a PDF, or its slabs do not add up to its totals)"
        if {sheet.block} != blocks:
            return None, f"the sheet is for block {sheet.block}, the scans for {sorted(blocks)}"
        return sheet, ""

    def _packing(self, sheet_id: str) -> dict:
        """The bundle's packing-list figures. A list that fails to load or has no readable summary is recorded
        as such (the adapter then holds the bundle's stock rather than use the site's figure in its place)."""
        blank = {"packing_status": "", "packing_available": "", "packing_sold": ""}
        if not sheet_id:
            return blank
        try:
            counts = _packing_counts(self.get(PACKING_LIST_CSV.format(id=sheet_id)).text)
        except Exception as exc:
            self.record_failure("packing_list", id=sheet_id, error=str(exc))
            return {**blank, "packing_status": "failed"}
        if counts is None:
            return {**blank, "packing_status": "unreadable"}
        return {"packing_status": "ok", "packing_available": counts[0], "packing_sold": counts[1]}

    def parse_product(self, item: dict) -> Optional[dict]:
        try:
            detail = _parse_product_page(self.get(item["url"]).text)
        except Exception as exc:
            self.record_failure("detail", product_id=item["product_id"], url=item["url"], error=str(exc))
            self.note_detail(ok=False)   # feeds the delist gate's detail-failure ratio
            detail = None
        else:
            self.note_detail(ok=True)
        facts = detail or {}
        # Every Drive folder is read regardless of the photo ceiling (a listing is one request; the ceiling
        # bounds photo processing, not the evidence a lot needs).
        listings = [self._drive_files(folder_id) for folder_id in facts.get("drive_folders", [])]
        folders = [files for files in listings if files is not None]
        images, drive_count = _photo_urls(facts.get("image_urls", []), [f for files in folders for f in files])
        pieces, sheet_problem = self._pieces(folders)
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
            "piece_scans": json.dumps(pieces) if pieces else "",
            **self._packing(facts.get("packing_list", "")),
            "image_urls": images,   # base keeps these as source links
        }
        # What this run could not read is unknown, not absent: HOLD the row for a retry next scrape rather than
        # let derive default a fabricated size, or let a lot fall back to its bundle's size or to one bundle.
        hold = ("product page fetch failed" if detail is None
                else "a Drive folder failed to list: its scans and block sheet are unknown" if None in listings
                else f"block sheet: {sheet_problem}" if sheet_problem else "")
        if hold:
            self.mark_fetch_failed(row, "dims", product_id=item["product_id"], url=item["url"], error=hold)
        return row


if __name__ == "__main__":
    SentasScraper().run()
