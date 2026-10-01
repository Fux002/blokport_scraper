"""Sentas Marble scraper: the page parsers are pinned on real markup fragments, and the two recoverable
failure paths behave as the admission checklist demands (a failed product page HOLDS the row via
fetch_failed; a failed variety grid marks the run INCOMPLETE so nothing is delisted off it). The adapter's
per-piece weight is the one source-specific conversion."""

from __future__ import annotations

import polars as pl
import pytest

from scrapers import sentas
from scrapers.sentas import SentasScraper
from stone_pipeline.adapters.sentas import ADAPTER

PRODUCT_PAGE = """
<h1 class="tt-title">B1103 - 2CM CALACATTA GREEN POLISHED</h1>
<div class="product-dimensions">
  <span class="dimension-item-detail"><strong>Height:</strong>
    <span class="detail-pill"><span class="pill-cm">165 cm</span><span class="pill-sep">|</span><span class="pill-inch">65&quot;</span></span></span>
  <span class="dimension-item-detail"><strong>Width:</strong>
    <span class="detail-pill"><span class="pill-cm">255 cm</span><span class="pill-sep">|</span><span class="pill-inch">100.4&quot;</span></span></span>
</div>
<div class="tt-review">( BH-12                        )                     </div>
<div class="tt-contact-info border"><img src="upload/icon_sqm.gif" style="width: 100px;">
  <p class="mt-0 mb-2" style="line-height: 16px; font-weight: 600;">
     54.00 sqm<br><span style="font-size: 11px;">581.25 sqft</span></p></div>
<div class="tt-contact-info border"><img src="upload/icon_weight.gif" style="width: 100px;">
  <p class="mt-0 mb-2" style="line-height: 16px; font-weight: 600;">
     3,088.00 kg<br><span style="font-size: 11px;">6,807.87 lbs</span></p></div>
<div class="tt-contact-info border"><img src="upload/icon_pieces.gif" style="width: 100px;">
  <p class="mt-0 mb-2" style="line-height: 16px; font-weight: 600;">12<br><span>pieces</span></p></div>
<a href="https://drive.google.com/drive/folders/1MDLpN6lV-fNaX99aQFkQDfJCvPpdzIYj" target="_blank">Drive Link</a>
<a href="https://drive.google.com/drive/folders/1pEVhFpCHw9pCmSAp-ojcjWbsRWEtuCBN?usp=drive_link" target="_blank">Scanner Photo</a>
<a href="https://drive.google.com/drive/folders/1MDLpN6lV-fNaX99aQFkQDfJCvPpdzIYj" class="btn-link">Drive Link</a>
<div class="tt-add-info"><ul>
  <li><span>Surface Finish:</span>
      Polished                                 </li>
  <li><span>Thickness:</span>
      2 CM                                 </li>
</ul></div>
<ul id="smallGallery" class="tt-slick-button-vertical slick-animated-show-js">
  <li><a class="zoomGalleryActive" href="#" data-image="upload/urunler/_b5e06b0cac.jpg.webp" data-zoom-image="upload/urunler/_b5e06b0cac.jpg.webp"><img src="upload/urunler/_b5e06b0cac.jpg.webp"></a></li>
  <li><a href="#" data-image="upload/urunler/_0edf12bc3c.jpg.webp" data-zoom-image="upload/urunler/_0edf12bc3c.jpg.webp"><img src="upload/urunler/_0edf12bc3c.jpg.webp"></a></li>
  <li><a href="#" data-image="upload/urunler/_b5e06b0cac.jpg.webp" data-zoom-image="upload/urunler/_b5e06b0cac.jpg.webp"><img src="upload/urunler/_b5e06b0cac.jpg.webp"></a></li>
</ul>
<div class="tt-product"><img data-src="upload/urunler/_similar_product.jpg.webp"></div>
"""

STOCKS_PAGE = """
<div class="tt-product"><div class="tt-image-box">
  <a href="https://sentasmarble.com/en/slabs/calacatta-viola">
    <span class="tt-img"><img src="images/loader-02.svg" data-src="thumb.php?src=upload/urunler/_a.jpg.webp"></span>
  </a></div>
  <div class="tt-description"><h2 class="tt-title"><a href="https://sentasmarble.com/en/slabs/calacatta-viola">Calacatta Viola</a></h2></div>
</div>
<div class="tt-product"><div class="tt-image-box">
  <a href="https://sentasmarble.com/en/slabs/afyon-white"><span class="tt-img"><img src="x.svg"></span></a></div>
  <div class="tt-description"><h2 class="tt-title"><a href="https://sentasmarble.com/en/slabs/afyon-white">Afyon White</a></h2></div>
</div>
"""

GRID_PAGE = """
<div class="col-6 col-md-4 tt-col-item"><div class="tt-product"><div class="tt-image-box">
  <a class="tt-btn-quickview modalisec" data-toggle="modal" data-tooltip="Quick View"
     data-target="#ModalquickView" href="javascript:;" data-id="1734" data-caption="B5419 - 2 CM Calacatta Viola Honed" data-tposition="left"></a>
  <a href="https://sentasmarble.com/en/product/b5419-2-cm-calacatta-viola-honed"><span class="tt-img"><img src="images/loader-02.svg"></span>
    <span class="tt-label-location"><span class="tt-label-sale">Stock: 35</span></span></a>
</div></div></div>
<div class="col-6 col-md-4 tt-col-item"><div class="tt-product"><div class="tt-image-box">
  <a class="tt-btn-quickview modalisec" data-toggle="modal" href="javascript:;" data-id="1718" data-caption="B6394 - 2 CM Calacatta Viola Honed" data-tposition="left"></a>
  <a href="https://sentasmarble.com/en/product/b6394-2-cm-calacatta-viola-honed"><span class="tt-img"></span></a>
</div></div></div>
"""

DRIVE_LISTING = """
<div class="flip-entry" id="entry-1rteWYDVmqrkmkWLKOR5AiKHuklEtAAwe"><div class="flip-entry-info"><div class="flip-entry-title">1859.pdf</div></div></div>
<div class="flip-entry" id="entry-1tB4t8gvpTUEWN4cxvRRXZP5jEWdZZNcx"><div class="flip-entry-info"><div class="flip-entry-title">custom_1859_1.jpg</div></div></div>
<div class="flip-entry" id="entry-1tNgMcDiV7M7r7nRUd0VbTN6CZg_5WgRI"><div class="flip-entry-info"><div class="flip-entry-title">custom_1859_10.JPG</div></div></div>
"""


class _Resp:
    def __init__(self, text: str):
        self.text = text


# -- product page parser ------------------------------------------------------------------------------------

def test_product_page_parses_every_fact_and_never_the_vendor_code():
    d = sentas._parse_product_page(PRODUCT_PAGE)
    assert d["name"] == "B1103 - 2CM CALACATTA GREEN POLISHED"
    assert (d["height_cm"], d["width_cm"]) == ("165 cm", "255 cm")       # unit captured with the value
    assert (d["total_sqm"], d["total_kg"], d["pieces"]) == ("54.00", "3088.00", "12")   # thousands sep gone
    assert (d["finish"], d["thickness"]) == ("Polished", "2 CM")
    # gallery only (not the similar-products carousel), deduped, absolute, in page order
    assert d["image_urls"] == ["https://sentasmarble.com/upload/urunler/_b5e06b0cac.jpg.webp",
                               "https://sentasmarble.com/upload/urunler/_0edf12bc3c.jpg.webp"]
    # both folders once each, Drive Link before Scanner Photo, query string stripped
    assert d["drive_folders"] == ["1MDLpN6lV-fNaX99aQFkQDfJCvPpdzIYj", "1pEVhFpCHw9pCmSAp-ojcjWbsRWEtuCBN"]
    assert "BH-12" not in str(d)


def test_product_page_without_the_elements_is_empty_not_guessed():
    d = sentas._parse_product_page("<html><body>maintenance</body></html>")
    assert d["name"] == "" and d["height_cm"] == "" and d["pieces"] == "" and d["finish"] == ""
    assert d["image_urls"] == [] and d["drive_folders"] == []


# -- listing parsers -----------------------------------------------------------------------------------------

def test_stocks_page_yields_each_variety_once_with_its_title(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp(STOCKS_PAGE))
    # the card's image anchor has no text; the title anchor names the variety -- one entry per url
    assert s._varieties() == [("Calacatta Viola", "https://sentasmarble.com/en/slabs/calacatta-viola"),
                              ("Afyon White", "https://sentasmarble.com/en/slabs/afyon-white")]


def test_variety_grid_yields_bundle_id_caption_and_url(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp("<script>var kategoriID = '11';</script>"))
    posted: list = []
    monkeypatch.setattr(s, "post", lambda url, **kw: posted.append(kw["data"]) or _Resp(GRID_PAGE))
    bundles = s._bundles("https://sentasmarble.com/en/slabs/calacatta-viola")
    assert posted == [{"kategoriID": "11", "sirala": "1"}]
    assert bundles == [
        ("1734", "B5419 - 2 CM Calacatta Viola Honed", "https://sentasmarble.com/en/product/b5419-2-cm-calacatta-viola-honed"),
        ("1718", "B6394 - 2 CM Calacatta Viola Honed", "https://sentasmarble.com/en/product/b6394-2-cm-calacatta-viola-honed"),
    ]


def test_variety_page_without_a_grid_id_fails_loud(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp("<html>no script</html>"))
    with pytest.raises(RuntimeError, match="kategoriID"):
        s._bundles("https://sentasmarble.com/en/slabs/x")


def test_a_failed_variety_grid_marks_the_run_incomplete_and_continues(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_type_map", lambda: {"calacattaviola": "Marble"})
    monkeypatch.setattr(s, "_varieties", lambda: [("Calacatta Viola", "u1"), ("Afyon White", "u2")])

    def bundles(url):
        if url == "u1":
            raise RuntimeError("GET failed after 5 tries")
        return [("1733", "B5805 - 2 CM Afyon White Honed", "p2")]
    monkeypatch.setattr(s, "_bundles", bundles)
    items = list(s.list_products())
    assert items == [{"product_id": "1733", "caption": "B5805 - 2 CM Afyon White Honed", "url": "p2",
                      "variety": "Afyon White", "variety_url": "u2", "stone_type": ""}]
    assert s._incomplete is True                           # unknown bundles are never delisted off this run
    assert [f["kind"] for f in s._failures] == ["variety"]


def test_type_map_keys_collection_captions_by_normalised_name(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    grids = {"1": 'data-caption="Bianco Venato" data-caption="Camelot  Grey"', "2": 'data-caption="Red Travertine"', "3": ""}
    monkeypatch.setattr(s, "post", lambda url, **kw: _Resp(grids[kw["data"]["kategoriID"]]))
    assert s._type_map() == {"biancovenato": "Marble", "camelotgrey": "Marble", "redtravertine": "Travertine"}


# -- images: gallery first, Drive appended, capped -------------------------------------------------------

def test_drive_listing_keeps_images_and_skips_the_packing_pdf(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp(DRIVE_LISTING))
    assert s._drive_images("f") == ["https://lh3.googleusercontent.com/d/1tB4t8gvpTUEWN4cxvRRXZP5jEWdZZNcx=s2000",
                                    "https://lh3.googleusercontent.com/d/1tNgMcDiV7M7r7nRUd0VbTN6CZg_5WgRI=s2000"]


def test_a_failed_drive_listing_contributes_nothing_and_never_holds(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)

    def boom(url, **kw):
        raise RuntimeError("GET failed after 5 tries")
    monkeypatch.setattr(s, "get", boom)
    assert s._drive_images("f") == []
    assert [f["kind"] for f in s._failures] == ["drive"]


def test_images_are_gallery_then_drive_capped_at_the_slot_count(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_drive_images", lambda fid: [f"{fid}-{i}" for i in range(20)])
    gallery = ["g1", "g2"]
    urls, drive_used = s._images({"image_urls": gallery, "drive_folders": ["A", "B"]})
    assert len(urls) == sentas.MAX_IMAGES and urls[:2] == gallery and urls[2:] == [f"A-{i}" for i in range(sentas.MAX_IMAGES - 2)]
    assert drive_used == sentas.MAX_IMAGES - 2
    # a bundle already at the cap never lists a folder
    monkeypatch.setattr(s, "_drive_images", lambda fid: pytest.fail("folder listed past the cap"))
    full = [f"g{i}" for i in range(sentas.MAX_IMAGES)]
    assert s._images({"image_urls": full, "drive_folders": ["A"]}) == (full, 0)


# -- parse_product: the detail fetch and its failure path -------------------------------------------------

_ITEM = {"product_id": "1734", "caption": "B5419 - 2 CM Calacatta Viola Honed", "url": "p",
         "variety": "Calacatta Viola", "variety_url": "v", "stone_type": "Marble"}


def test_parse_product_rows_the_page_facts(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp(PRODUCT_PAGE))
    monkeypatch.setattr(s, "_drive_images", lambda fid: [f"{fid}-1"])
    row = s.parse_product(dict(_ITEM))
    assert row["name"] == "B1103 - 2CM CALACATTA GREEN POLISHED" and row["variety"] == "Calacatta Viola"
    assert (row["height_cm"], row["width_cm"], row["pieces"], row["total_kg"]) == ("165 cm", "255 cm", "12", "3088.00")
    assert row["image_urls"][:2] == ["https://sentasmarble.com/upload/urunler/_b5e06b0cac.jpg.webp",
                                     "https://sentasmarble.com/upload/urunler/_0edf12bc3c.jpg.webp"]
    assert row["image_urls"][2:] == ["1MDLpN6lV-fNaX99aQFkQDfJCvPpdzIYj-1", "1pEVhFpCHw9pCmSAp-ojcjWbsRWEtuCBN-1"]
    assert row["drive_image_count"] == 2 and "fetch_failed" not in row
    assert (s._detail_ok, s._detail_failed) == (1, 0)


def test_a_failed_product_page_holds_the_row_for_retry(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)

    def boom(url, **kw):
        raise RuntimeError("GET failed after 5 tries")
    monkeypatch.setattr(s, "get", boom)
    row = s.parse_product(dict(_ITEM))
    assert row["product_id"] == "1734" and row["variety"] == "Calacatta Viola"   # identity kept: no delist
    assert row["height_cm"] == "" and row["image_urls"] == []
    assert row[SentasScraper.FETCH_FAILED_COL] == "dims"                          # derive holds, never defaults
    assert (s._detail_ok, s._detail_failed) == (0, 1)
    assert sorted(f["kind"] for f in s._failures) == ["detail", "dims"]


def test_declarations():
    assert SentasScraper.category == "slab" and SentasScraper.dimension_unit == "cm"
    assert SentasScraper.use_curl_cffi is False and SentasScraper.proxy_capability == ""


# -- adapter ---------------------------------------------------------------------------------------------

def _adapt(**overrides):
    record = {"product_id": "1734", "url": "p", "variety": "Afyon White", "stone_type": "", "finish": "Honed",
              "thickness": "2 CM", "height_cm": "190 cm", "width_cm": "328 cm", "total_sqm": "271.85",
              "total_kg": "14951.00", "pieces": "44", "image_urls": "a | b", "format": "slab",
              "scrape_timestamp": "", **overrides}
    rows = ADAPTER.adapt(pl.DataFrame([record]))
    assert len(rows) == 1
    return rows[0]


def test_adapter_maps_faces_weight_per_piece_and_leaves_quality_blank():
    row = _adapt()
    assert row.raw_dimensions == "length=328 cm;height=190 cm"      # long face -> length, short -> raw height
    assert row.raw_thickness == "2 CM" and row.raw_slab_count == "44" and row.raw_total_m2 == "271.85"
    assert row.raw_weight == "339.795"                               # 14951 kg / 44 pieces
    assert row.raw_quality == "" and row.raw_type == "" and row.raw_color == "White"
    assert row.raw_name == row.variety_match_key == "Afyon White" and row.raw_format == "slab"
    assert row.raw_image_urls == ["a", "b"] and row.src_url == "p" and row.src_natural_key == "1734"


def test_adapter_weight_is_blank_when_a_figure_is_missing_or_zero():
    assert _adapt(total_kg="").raw_weight == ""
    assert _adapt(pieces="0").raw_weight == ""
    assert _adapt(total_kg="abc").raw_weight == ""
