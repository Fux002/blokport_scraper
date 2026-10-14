"""Sentas Marble scraper: the page parsers are pinned on real markup fragments of the current site (the
JSON-paged stocks index and category listings, the product page), and the two recoverable failure paths
behave as the admission checklist demands (a failed product page HOLDS the row via fetch_failed; a failed
variety listing marks the run INCOMPLETE so nothing is delisted off it). The adapter's per-piece weight is
the one source-specific conversion."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import polars as pl
import pytest

from scrapers import sentas
from scrapers.sentas import SentasScraper
from stone_pipeline.adapters.sentas import ADAPTER

PRODUCT_PAGE = """
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}</script>
<h1 class="product-title">
    B6392 - 2 CM Calacatta Viola Honed
</h1>
<div class="product-dimensions my-3">
  <div class="dimension-item-detail"><span class="dim-label">Height :</span>
    <span class="detail-pill"><span class="pill-cm">198 cm</span><span class="pill-sep">|</span><span class="pill-inch">78&quot;</span></span></div>
  <div class="dimension-item-detail"><span class="dim-label">Width :</span>
    <span class="detail-pill"><span class="pill-cm">317 cm</span><span class="pill-sep">|</span><span class="pill-inch">124.8&quot;</span></span></div>
  <div class="dimension-item-detail"><span class="dim-label">Thickness :</span>
    <span class="detail-pill"><span class="pill-cm">2 cm</span><span class="pill-sep">|</span><span class="pill-inch">0.8&quot;</span></span></div>
</div>
<div class="stock-stat-box"><img src="https://sentasmarble.com/assets/web/upload/icon_sqm.gif" alt="sqm">
  <div class="fw-bold text-dark mt-1 stat-main-val" style="font-size: 13px;">
      489.57 sqm
  </div><div class="text-muted stat-sub-val">5,269.68 sqft</div></div>
<div class="stock-stat-box"><img src="https://sentasmarble.com/assets/web/upload/icon_weight.gif" alt="weight">
  <div class="fw-bold text-dark mt-1 stat-main-val">
      26,926.35 kg
  </div></div>
<div class="stock-stat-box"><img src="https://sentasmarble.com/assets/web/upload/icon_pieces.gif" alt="pieces">
  <div class="fw-bold text-dark mt-1 stat-main-val">78</div><div class="stat-sub-val">Pieces</div></div>
<a href="https://docs.google.com/spreadsheets/d/1XI/edit?usp=drive_web" class="action-card-item card-drive"><div class="action-card-title">Packing List</div><div class="action-card-sub">Google Drive Folder</div></a>
<a href="https://drive.google.com/drive/u/0/folders/1mqZ47Z0KmbZ3B9o1st9w0FCzdr8_h2QT" target="_blank" class="action-card-item card-drive"><div class="action-card-title">Google Drive</div><div class="action-card-sub">Google Drive Folder</div></a>
<a href="https://drive.google.com/open?id=1O7Os9fzTHYah1s51-5SndIZSbxJ2PUWw&amp;usp=drive_fs" target="_blank" class="action-card-item card-drive"><div class="action-card-title">Scanner Photo</div><div class="action-card-sub">Google Drive Folder</div></a>
<a href="https://api.whatsapp.com/send?phone=1" class="action-card-item card-whatsapp"><div>WhatsApp</div></a>
<table><tr><td class="spec-label text-muted" style="width: 140px;">Thickness:</td><td class="spec-val fw-bold">
      2 cm
  </td></tr>
<tr><td class="spec-label text-muted">Surface Finish:</td><td class="spec-val fw-bold">
                              Honed
  </td></tr>
<tr><td class="spec-label text-muted">Colors:</td><td class="spec-val fw-bold">
      <span class="d-inline-block rounded-circle" style="background-color: #16b14c;"></span>
      Green,
      <span class="d-inline-block rounded-circle" style="background-color: #a90ec8;"></span>
      Purple
  </td></tr></table>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "B6392 - 2 CM Calacatta Viola Honed",
 "sku": "SM-7149", "image": ["https://sentasmarble.com/upload/media/images/2026/10/a.webp", "https://sentasmarble.com/upload/media/images/2026/10/b.webp", "https://sentasmarble.com/upload/media/images/2026/10/a.webp"]}</script>
<div class="similar-products"><img src="https://sentasmarble.com/upload/media/images/2026/10/other.webp"></div>
"""

STOCKS_PAGE_HTML = """
<div class="col-6 col-md-4 col-lg-4 tt-col-item"><div class="slab-category-card">
  <a href="https://sentasmarble.com/en/categories/new-slabs" class="d-block w-100 h-100">
    <img src="https://sentasmarble.com/thumb?src=x&amp;w=900" alt="New Slabs" width="380" loading="lazy"></a>
  <h3><a href="https://sentasmarble.com/en/categories/new-slabs" class="slab-card-title">New Slabs <span>(12)</span></a></h3>
</div></div>
<div class="col-6 col-md-4 col-lg-4 tt-col-item"><div class="slab-category-card">
  <a href="https://sentasmarble.com/en/categories/calacatta-viola" class="d-block w-100 h-100">
    <img src="https://sentasmarble.com/thumb?src=y&amp;w=900" alt="Calacatta Viola" width="380" loading="lazy"></a>
  <h3><a href="https://sentasmarble.com/en/categories/calacatta-viola" class="slab-card-title">Calacatta Viola <span>(60)</span></a></h3>
</div></div>
<div class="col-6 col-md-4 col-lg-4 tt-col-item"><div class="slab-category-card">
  <a href="https://sentasmarble.com/en/categories/afyon-white" class="d-block w-100 h-100">
    <img src="https://sentasmarble.com/thumb?src=z&amp;w=900" alt="Afyon White"></a>
</div></div>
"""

CARDS_HTML = """
<div class="tt-product-listing tt-col-two" id="categoryProductsListing">
  <div class="tt-col-item"><div class="slab-stock-card">
    <a href="https://sentasmarble.com/en/products/b6392-2-cm-calacatta-viola-honed" class="d-block">
      <img src="https://sentasmarble.com/thumb?src=a&amp;w=900" alt="B6392 - 2 CM Calacatta Viola Honed" width="380" loading="lazy">
      <span>Stock: 78</span><span>B6392</span></a>
    <button onclick="openProductQuickView('7149', 'en')">Quick View</button>
  </div></div>
  <div class="tt-col-item"><div class="slab-stock-card">
    <a href="https://sentasmarble.com/en/products/b1603-3-cm-calacatta-viola-polished" class="d-block">
      <img src="https://sentasmarble.com/thumb?src=b&amp;w=900" alt="B1603 - 3 CM Calacatta Viola Polished"></a>
    <button onclick="openProductQuickView('6928', 'en')">Quick View</button>
  </div></div>
</div>
"""

DRIVE_LISTING = """
<div class="flip-entry" id="entry-1rteWYDVmqrkmkWLKOR5AiKHuklEtAAwe"><div class="flip-entry-info"><div class="flip-entry-title">6392.pdf</div></div></div>
<div class="flip-entry" id="entry-1tB4t8gvpTUEWN4cxvRRXZP5jEWdZZNcx"><div class="flip-entry-info"><div class="flip-entry-title">custom_6392_1.jpg</div></div></div>
<div class="flip-entry" id="entry-1tNgMcDiV7M7r7nRUd0VbTN6CZg_5WgRI"><div class="flip-entry-info"><div class="flip-entry-title">custom_6392_10.JPG</div></div></div>
"""


class _Resp:
    def __init__(self, text: str = "", payload: dict | None = None, content: bytes = b""):
        self.text, self._payload, self.content = text, payload, content

    def json(self):
        return self._payload


def _paged(pages: dict[str, list[dict]]):
    """A `get` stub serving JSON pages per listing url: pages[url][page-1]."""
    def get(url, params=None, **kw):
        return _Resp(payload=pages[url][int(params["page"]) - 1])
    return get


# -- product page parser ------------------------------------------------------------------------------------

def test_product_page_parses_every_fact():
    d = sentas._parse_product_page(PRODUCT_PAGE)
    assert d["name"] == "B6392 - 2 CM Calacatta Viola Honed"
    assert (d["height_cm"], d["width_cm"]) == ("198 cm", "317 cm")       # unit captured with the value
    assert (d["total_sqm"], d["total_kg"], d["pieces"]) == ("489.57", "26926.35", "78")   # thousands sep gone
    assert (d["finish"], d["thickness"], d["color"]) == ("Honed", "2 cm", "Green, Purple")
    # the JSON-LD Product image list only (not the similar-products carousel), deduped, in order
    assert d["image_urls"] == ["https://sentasmarble.com/upload/media/images/2026/10/a.webp",
                               "https://sentasmarble.com/upload/media/images/2026/10/b.webp"]
    # both Drive photo folders, in page order, in either url form; the packing-list sheet is not one
    assert d["drive_folders"] == ["1mqZ47Z0KmbZ3B9o1st9w0FCzdr8_h2QT", "1O7Os9fzTHYah1s51-5SndIZSbxJ2PUWw"]


def test_product_page_without_the_elements_is_empty_not_guessed():
    d = sentas._parse_product_page("<html><body>maintenance</body></html>")
    assert d["name"] == "" and d["height_cm"] == "" and d["pieces"] == "" and d["finish"] == "" and d["color"] == ""
    assert d["image_urls"] == [] and d["drive_folders"] == []


def test_thickness_falls_back_to_the_dimension_pill_when_the_spec_row_is_absent():
    page = PRODUCT_PAGE.replace('<td class="spec-label text-muted" style="width: 140px;">Thickness:</td>', "<td>x</td>")
    assert sentas._parse_product_page(page)["thickness"] == "2 cm"


# -- listing parsers -----------------------------------------------------------------------------------------

def test_stocks_index_yields_each_variety_once_across_pages_and_skips_the_grouping_cards(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    page2 = (STOCKS_PAGE_HTML.replace("afyon-white", "pink-onyx").replace("Afyon White", "Pink Onyx")
             .replace("new-slabs", "warehouse-special-products").replace("New Slabs", "Warehouse Special Products"))
    monkeypatch.setattr(s, "get", _paged({sentas.STOCKS_URL: [
        {"html": STOCKS_PAGE_HTML, "hasMore": True, "nextPage": 2, "total": 4},
        {"html": page2, "hasMore": False, "nextPage": 3, "total": 4}]}))
    assert s._varieties() == [("Calacatta Viola", "https://sentasmarble.com/en/categories/calacatta-viola"),
                              ("Afyon White", "https://sentasmarble.com/en/categories/afyon-white"),
                              ("Pink Onyx", "https://sentasmarble.com/en/categories/pink-onyx")]


def test_variety_listing_yields_bundle_id_caption_and_url_across_pages(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    url = "https://sentasmarble.com/en/categories/calacatta-viola"
    second = CARDS_HTML.replace("7149", "7001").replace("b6392-2-cm", "b1-2-cm").replace("6928", "7002").replace("b1603-3-cm", "b2-3-cm")
    seen: list = []

    def get(u, params=None, **kw):
        seen.append((u, dict(params), kw.get("headers")))
        return _Resp(payload=[{"cards_html": CARDS_HTML, "hasMore": True, "nextPage": 2, "total": 4},
                              {"cards_html": second, "hasMore": False, "nextPage": 3, "total": 4}][int(params["page"]) - 1])
    monkeypatch.setattr(s, "get", get)
    bundles = s._bundles(url)
    assert [b[0] for b in bundles] == ["7149", "6928", "7001", "7002"]
    assert bundles[0] == ("7149", "B6392 - 2 CM Calacatta Viola Honed",
                          "https://sentasmarble.com/en/products/b6392-2-cm-calacatta-viola-honed")
    assert seen == [(url, {"ajax": "1", "page": "1"}, sentas.AJAX_HEADERS), (url, {"ajax": "1", "page": "2"}, sentas.AJAX_HEADERS)]


def test_a_listing_short_of_its_advertised_total_fails_loud(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", _paged({"u": [{"cards_html": CARDS_HTML, "hasMore": False, "total": 60}]}))
    with pytest.raises(RuntimeError, match="2 cards for an advertised total of 60"):
        s._bundles("u")


def test_a_failed_variety_listing_marks_the_run_incomplete_and_continues(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_type_map", lambda: {"calacattaviola": "Marble"})
    monkeypatch.setattr(s, "_varieties", lambda: [("Calacatta Viola", "u1"), ("Afyon White", "u2")])

    def bundles(url):
        if url == "u1":
            raise RuntimeError("GET failed after 5 tries")
        return [("7074", "B5805 - 2 CM Afyon White Honed", "p2")]
    monkeypatch.setattr(s, "_bundles", bundles)
    items = list(s.list_products())
    assert items == [{"product_id": "7074", "caption": "B5805 - 2 CM Afyon White Honed", "url": "p2",
                      "variety": "Afyon White", "variety_url": "u2", "stone_type": ""}]
    assert s._incomplete is True                           # unknown bundles are never delisted off this run
    assert [f["kind"] for f in s._failures] == ["variety"]


def test_a_bundle_listed_under_two_cards_is_yielded_once_under_the_first(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_type_map", lambda: {})
    monkeypatch.setattr(s, "_varieties", lambda: [("Baran Grey", "u1"), ("Other Group", "u2")])
    monkeypatch.setattr(s, "_bundles", lambda url: [("1739", "4500 - 2 CM Baran Grey Honed", "p")])
    items = list(s.list_products())
    assert [(i["product_id"], i["variety"]) for i in items] == [("1739", "Baran Grey")]


def test_type_map_keys_collection_card_titles_by_normalised_name(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    card = '<div class="tt-col-item"><a href="https://sentasmarble.com/en/products/{slug}" class="d-block"><img src="t" alt="{name}"></a></div>'
    pages = {sentas.CATEGORY_URL.format(slug="marble-collection"): [
                 {"cards_html": card.format(slug="bianco-venato", name="Bianco Venato"), "hasMore": True},
                 {"cards_html": card.format(slug="camelot-grey", name="Camelot  Grey"), "hasMore": False}],
             sentas.CATEGORY_URL.format(slug="travertine-collection"): [
                 {"cards_html": card.format(slug="red-travertine", name="Red Travertine"), "hasMore": False}],
             sentas.CATEGORY_URL.format(slug="onyx-collection"): [{"cards_html": "", "hasMore": False}]}
    monkeypatch.setattr(s, "get", _paged(pages))
    assert s._type_map() == {"biancovenato": "Marble", "camelotgrey": "Marble", "redtravertine": "Travertine"}


# -- Drive folders: every file listed with its title ----------------------------------------------------

def test_drive_listing_returns_every_file_with_its_id(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp(DRIVE_LISTING))
    assert s._drive_files("f") == [("6392.pdf", "1rteWYDVmqrkmkWLKOR5AiKHuklEtAAwe"),
                                   ("custom_6392_1.jpg", "1tB4t8gvpTUEWN4cxvRRXZP5jEWdZZNcx"),
                                   ("custom_6392_10.JPG", "1tNgMcDiV7M7r7nRUd0VbTN6CZg_5WgRI")]


def test_a_drive_folder_that_is_not_shared_is_empty_not_unknown(tmp_path, monkeypatch):
    # a private folder answers 401/403: its contents cannot be read on any run, so nothing is held for it
    s = SentasScraper(data_dir=tmp_path)
    seen: list = []

    def get(url, **kw):
        seen.append(kw.get("missing_statuses"))
        return None                                      # the base's answer to a definitive status
    monkeypatch.setattr(s, "get", get)
    assert s._drive_files("f") == []
    assert seen == [sentas.DRIVE_FOLDER_NOT_SHARED] and {401, 403, 404} <= set(sentas.DRIVE_FOLDER_NOT_SHARED)
    assert [f["kind"] for f in s._failures] == ["drive_not_shared"]


def test_a_failed_drive_listing_is_unknown_not_empty(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)

    def boom(url, **kw):
        raise RuntimeError("GET failed after 5 tries")
    monkeypatch.setattr(s, "get", boom)
    assert s._drive_files("f") is None
    assert [f["kind"] for f in s._failures] == ["drive"]


# -- photos: gallery first, every Drive image appended (capped) -------------------------------------------

_PHONE = [("20261009_113443.jpg", "a1"), ("20261009_113520.jpg", "a2"), ("B6392.xlsx", "x1")]
_SCANS = [("custom_6392_10.jpg", "s10"), ("custom_6392_2.jpg", "s2"), ("custom_6392_2b.jpg", "x"), ("6392.pdf", "pdf")]


def _cdn(file_id):
    return sentas.DRIVE_IMAGE.format(id=file_id)


def test_photos_take_the_gallery_then_every_drive_image():
    urls, drive_used = sentas._photo_urls(["g1", "g2"], [*_PHONE, *_SCANS])
    assert urls == ["g1", "g2", *map(_cdn, ["a1", "a2", "s10", "s2", "x"])] and drive_used == 5


def test_the_ceiling_caps_the_bundle_photos():
    many = [(f"p{i}.jpg", f"p{i}") for i in range(sentas.MAX_IMAGES + 5)]
    urls, drive_used = sentas._photo_urls(["g1", "g2"], many)
    assert len(urls) == sentas.MAX_IMAGES and urls[:2] == ["g1", "g2"] and drive_used == sentas.MAX_IMAGES - 2


# -- the block sheet: the scanner's cutting list, one measured entry per slab -----------------------------

BLOCK_SHEET = (Path(__file__).parent / "fixtures" / "sentas_block_sheet_6208B.txt").read_text()

# Real entry shapes: two void entries (no size, weight 0.0 kg*) and an older sheet's starred but MEASURED slab.
_SHEET = """BLOCK SHEET

Date:        09/10/2026                  Block:       6392
Material:    CALACATTA VIOLA             Weight: {kg} Kg   Area:     {m2} m2


 15                                                               16




 SIZE 0 x 0 x 2.00                       QA       -               SIZE 305 x 183 x 2.00                  QA   -
 FINS Honed               A   0.000 m2   W        0.0 kg*         FINS Honed              A   5.582 m2   W    315.0 kg


 81                                                               2




 SIZE -5 x -5 x 2.00                     QA       -               SIZE 218 x 167 x 2.00                  QA   -
 FINS Polished            A   0.003 m2   W        0.0 kg*         FINS Honed              A   3.641 m2   W    270.6 kg*


Defects:    ---

                                                              1
"""


def test_the_block_sheet_gives_each_slab_its_own_measured_size_and_weight():
    sheet = sentas._block_sheet(BLOCK_SHEET)
    assert sheet.block == "6208"
    assert sorted(sheet.slabs) == [4, 13, 20, 24, 34, 47, 49, 50, 66]     # exactly the bundle's nine scans
    assert sheet.slabs[4] == (299.0, 167.0, 260.0) and sheet.slabs[66] == (284.0, 171.0, 269.0)


def test_a_void_entry_is_not_a_measured_slab_and_a_starred_weight_is():
    sheet = sentas._block_sheet(_SHEET.format(kg="585.6", m2="9.226"))
    assert sheet.slabs == {16: (305.0, 183.0, 315.0), 2: (218.0, 167.0, 270.6)}


def test_a_page_titled_with_the_bare_block_number_is_not_a_row_of_slabs():
    # some sheets title every page with just the block number ("4720"), right above the page header
    page = _SHEET.format(kg="585.6", m2="9.226").replace("BLOCK SHEET", "6392")
    first, second = page.split(" 81 ")
    two_pages = first + "\n6392\n\nDate:        09/10/2026                  Block:       6392\n\n 81 " + second
    assert sentas._block_sheet(two_pages).slabs == {16: (305.0, 183.0, 315.0), 2: (218.0, 167.0, 270.6)}


@pytest.mark.parametrize("text", [
    _SHEET.format(kg="999.0", m2="9.226"),                                  # slabs do not add up to the header
    _SHEET.format(kg="585.6", m2="12.000"),
    _SHEET.format(kg="585.6", m2="9.226").replace(" SIZE -5 x -5 x 2.00", " SIZE x"),   # a label without its size
    _SHEET.format(kg="585.6", m2="9.226").replace("Block:       6392", ""),  # no block number
    "",
])
def test_a_sheet_that_cannot_be_read_with_certainty_is_none(text):
    assert sentas._block_sheet(text) is None


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="the PDF reader is not installed here")
def test_a_file_that_is_not_a_readable_pdf_converts_to_none():
    assert sentas._pdf_text(b"%PDF-1.4 truncated") is None


# -- the slab scans: measured from the block sheet beside them ----------------------------------------------

def _scraper(tmp_path, monkeypatch, folders, sheet_text=BLOCK_SHEET):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_drive_files", lambda fid: folders[fid])
    downloads: list = []

    def get(url, **kw):
        downloads.append(url)
        if sheet_text is None:
            raise RuntimeError("GET failed after 5 tries")
        return _Resp(content=b"%PDF-1.4 sheet")
    monkeypatch.setattr(s, "get", get)
    monkeypatch.setattr(sentas, "_pdf_text", lambda data: sheet_text)
    return s, downloads


_SCAN_FOLDER = [("custom_6208_4.jpg", "s4"), ("custom_6208_13.jpg", "s13"), ("custom_6208_99.jpg", "s99"),
                ("6208B.pdf", "sheet")]


def test_each_scan_carries_its_slab_s_measured_size_and_weight(tmp_path, monkeypatch):
    s, downloads = _scraper(tmp_path, monkeypatch, {"A": _PHONE, "B": _SCAN_FOLDER})
    pieces, problem = s._pieces([_PHONE, _SCAN_FOLDER])
    assert problem == "" and downloads == [sentas.DRIVE_FILE.format(id="sheet")]
    assert pieces == [
        {"position": 4, "image_urls": [_cdn("s4")], "length_cm": 299.0, "width_cm": 167.0, "weight_kg": 260.0},
        {"position": 13, "image_urls": [_cdn("s13")], "length_cm": 298.0, "width_cm": 170.0, "weight_kg": 261.0},
        {"position": 99, "image_urls": [_cdn("s99")]}]                      # not on the sheet: not measured


def test_scans_without_a_block_sheet_are_listed_unmeasured_and_nothing_is_held(tmp_path, monkeypatch):
    folder = [f for f in _SCAN_FOLDER if not f[0].endswith(".pdf")]
    s, downloads = _scraper(tmp_path, monkeypatch, {"B": folder})
    pieces, problem = s._pieces([folder])
    assert problem == "" and downloads == [] and all(set(p) == {"position", "image_urls"} for p in pieces)


@pytest.mark.parametrize("folder, sheet_text, problem", [
    (_SCAN_FOLDER, None, "download failed"),                                # transient: retried next scrape
    (_SCAN_FOLDER, "garbled", "unreadable"),
    ([("custom_7000_4.jpg", "s4"), ("7000.pdf", "sheet")], BLOCK_SHEET, "block 6208"),   # another block's sheet
    ([*_SCAN_FOLDER, ("6208-old.pdf", "old")], BLOCK_SHEET, "2 PDFs"),      # which one is the sheet is unknown
])
def test_a_block_sheet_that_cannot_be_used_is_a_problem_never_a_guess(tmp_path, monkeypatch, folder, sheet_text, problem):
    s, _ = _scraper(tmp_path, monkeypatch, {"B": folder}, sheet_text=sheet_text)
    pieces, found = s._pieces([folder])
    assert problem in found and all("length_cm" not in p for p in pieces)


# -- parse_product: the detail fetch and its failure paths ------------------------------------------------

_ITEM = {"product_id": "7149", "caption": "B6392 - 2 CM Calacatta Viola Honed", "url": "p",
         "variety": "Calacatta Viola", "variety_url": "v", "stone_type": "Marble"}

_PAGE_FOLDERS = {"1mqZ47Z0KmbZ3B9o1st9w0FCzdr8_h2QT": [("20261009_113443.jpg", "phone-1")],
                 "1O7Os9fzTHYah1s51-5SndIZSbxJ2PUWw": [("custom_6208_4.jpg", "scan-4"), ("custom_6208_13.jpg", "scan-13"),
                                                        ("6208B.pdf", "sheet")]}


def _page_scraper(tmp_path, monkeypatch, sheet_text=BLOCK_SHEET):
    s = SentasScraper(data_dir=tmp_path)

    def get(url, **kw):
        if url == sentas.DRIVE_FILE.format(id="sheet"):
            if sheet_text is None:
                raise RuntimeError("GET failed after 5 tries")
            return _Resp(content=b"%PDF-1.4 sheet")
        return _Resp(PRODUCT_PAGE)
    monkeypatch.setattr(s, "get", get)
    monkeypatch.setattr(s, "_drive_files", lambda fid: _PAGE_FOLDERS[fid])
    monkeypatch.setattr(sentas, "_pdf_text", lambda data: sheet_text)
    return s


def test_parse_product_rows_the_page_facts(tmp_path, monkeypatch):
    s = _page_scraper(tmp_path, monkeypatch)
    row = s.parse_product(dict(_ITEM))
    assert row["name"] == "B6392 - 2 CM Calacatta Viola Honed" and row["variety"] == "Calacatta Viola"
    assert (row["height_cm"], row["width_cm"], row["pieces"], row["total_kg"], row["color"]) == ("198 cm", "317 cm", "78", "26926.35", "Green, Purple")
    assert row["image_urls"][:2] == ["https://sentasmarble.com/upload/media/images/2026/10/a.webp",
                                     "https://sentasmarble.com/upload/media/images/2026/10/b.webp"]
    assert row["image_urls"][2:] == [_cdn("phone-1"), _cdn("scan-4"), _cdn("scan-13")]
    assert row["drive_image_count"] == 3 and "fetch_failed" not in row
    assert json.loads(row["piece_scans"]) == [
        {"position": 4, "image_urls": [_cdn("scan-4")], "length_cm": 299.0, "width_cm": 167.0, "weight_kg": 260.0},
        {"position": 13, "image_urls": [_cdn("scan-13")], "length_cm": 298.0, "width_cm": 170.0, "weight_kg": 261.0}]
    assert (s._detail_ok, s._detail_failed) == (1, 0)


def test_a_block_sheet_that_failed_holds_the_row_for_retry(tmp_path, monkeypatch):
    # the slabs' sizes are unknown this run, not absent: hold, never offer them on the bundle's size
    s = _page_scraper(tmp_path, monkeypatch, sheet_text=None)
    row = s.parse_product(dict(_ITEM))
    assert row[SentasScraper.FETCH_FAILED_COL] == "dims"
    assert [f["kind"] for f in s._failures] == ["dims"] and "block sheet" in s._failures[0]["error"]


def test_a_drive_folder_that_failed_to_list_holds_the_row_for_retry(tmp_path, monkeypatch):
    # its scans and block sheet are unknown this run: a lot must never fall back to its bundle on a blip
    s = _page_scraper(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_drive_files", lambda fid: None if fid == "1O7Os9fzTHYah1s51-5SndIZSbxJ2PUWw"
                        else _PAGE_FOLDERS[fid])
    row = s.parse_product(dict(_ITEM))
    assert row[SentasScraper.FETCH_FAILED_COL] == "dims" and "Drive folder" in s._failures[0]["error"]


def test_a_failed_product_page_holds_the_row_for_retry(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)

    def boom(url, **kw):
        raise RuntimeError("GET failed after 5 tries")
    monkeypatch.setattr(s, "get", boom)
    row = s.parse_product(dict(_ITEM))
    assert row["product_id"] == "7149" and row["variety"] == "Calacatta Viola"   # identity kept: no delist
    assert row["height_cm"] == "" and row["image_urls"] == [] and row["piece_scans"] == ""
    assert row[SentasScraper.FETCH_FAILED_COL] == "dims"                          # derive holds, never defaults
    assert (s._detail_ok, s._detail_failed) == (0, 1)
    assert sorted(f["kind"] for f in s._failures) == ["detail", "dims"]


def test_declarations():
    assert SentasScraper.category == "slab" and SentasScraper.dimension_unit == "cm"
    assert SentasScraper.use_curl_cffi is False and SentasScraper.proxy_capability == ""


# -- adapter ---------------------------------------------------------------------------------------------

def _adapt(**overrides):
    record = {"product_id": "7074", "url": "p", "variety": "Afyon White", "stone_type": "Marble", "finish": "Honed",
              "color": "", "thickness": "2 cm", "height_cm": "190 cm", "width_cm": "328 cm", "total_sqm": "271.85",
              "total_kg": "14951.70", "pieces": "44", "image_urls": "a | b", "format": "slab",
              "scrape_timestamp": "", **overrides}
    rows = ADAPTER.adapt(pl.DataFrame([record]))
    assert len(rows) == 1
    return rows[0]


def test_adapter_maps_faces_weight_per_piece_and_leaves_quality_blank():
    row = _adapt()
    assert row.raw_dimensions == "length=328 cm;height=190 cm"      # long face -> length, short -> raw height
    assert row.raw_thickness == "2 cm" and row.raw_slab_count == "44" and row.raw_total_m2 == "271.85"
    assert row.raw_weight == "339.811"                               # 14951.70 kg / 44 pieces
    assert row.raw_quality == "" and row.raw_type == "Marble"
    assert row.raw_name == row.variety_match_key == "Afyon White" and row.raw_format == "slab"
    assert row.raw_image_urls == ["a", "b"] and row.src_url == "p" and row.src_natural_key == "7074"


def test_adapter_colour_is_the_site_field_else_a_colour_word_in_the_name():
    assert _adapt(color="Green, Purple").raw_color == "Green, Purple"   # the shared resolver keeps the primary
    assert _adapt(color="").raw_color == "White"


def test_adapter_maps_each_scan_with_its_measured_size_and_the_printed_bundle_reference():
    scans = json.dumps([{"position": 2, "image_urls": ["https://lh3/x=s2000"], "length_cm": 299.0, "width_cm": 167.0,
                         "weight_kg": 260.5},
                        {"position": 7, "image_urls": ["https://lh3/y=s2000"]}])
    row = _adapt(caption="B6392 - 2 CM Calacatta Viola Honed", piece_scans=scans)
    assert json.loads(row.raw_pieces) == [
        {"position": 2, "image_urls": ["https://lh3/x=s2000"], "raw_dimensions": "length=299 cm;height=167 cm",
         "raw_weight": "260.5"},
        {"position": 7, "image_urls": ["https://lh3/y=s2000"]}]              # unmeasured: no size declared
    assert row.src_lot_label == "B6392"
    assert row.src_lot_ref is None                 # the loader takes the bundle's own key as the lot reference
    assert _adapt(caption="Calacatta Viola", piece_scans="").raw_pieces == "" \
        and _adapt(caption="Calacatta Viola").src_lot_label == ""


def test_adapter_weight_is_blank_when_a_figure_is_missing_or_zero():
    assert _adapt(total_kg="").raw_weight == ""
    assert _adapt(pieces="0").raw_weight == ""
    assert _adapt(total_kg="abc").raw_weight == ""


# -- real captured data: the loader turns exactly the fully scanned bundle into its pieces --------------

def test_the_captured_scanned_bundle_becomes_its_pieces_and_the_rest_stay_whole():
    from stone_pipeline.adapters import selftest
    from stone_pipeline.adapters.base import read_scrape_csv
    from stone_pipeline.config.sources import SourceConfig
    from stone_pipeline.stages import lots

    rows = ADAPTER.adapt(read_scrape_csv(selftest.fixture_dir("sentas") / "input.csv"))
    loaded = lots.load(rows, SourceConfig(source="sentas", source_code="sen"))
    pieces = [r for r in loaded if r.lot_code]
    assert len(loaded) == len(rows) - 1 + 9                         # bundle 2555 (9 measured scans) split
    assert {p.src_lot_ref for p in pieces} == {"2555"} and len({p.lot_code for p in pieces}) == 1
    assert pieces[0].lot_code.startswith("B6208B-")
    assert [p.lot_position for p in pieces] == [4, 13, 20, 24, 34, 47, 49, 50, 66]
    assert all(len(p.raw_image_urls) == 11 - 9 + 1 for p in pieces)   # own scan + the 2 shared photos
    # each slab its own size and weight, from the vendor's block sheet (the real B6208B sheet, pinned above)
    sheet = sentas._block_sheet(BLOCK_SHEET)
    assert [(p.raw_dimensions, p.raw_weight) for p in pieces] == [
        (f"length={sheet.slabs[n][0]:g} cm;height={sheet.slabs[n][1]:g} cm", f"{sheet.slabs[n][2]:g}")
        for n in (4, 13, 20, 24, 34, 47, 49, 50, 66)]
