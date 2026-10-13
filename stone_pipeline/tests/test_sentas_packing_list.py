"""Sentas packing list: where a bundle has one, it is the stock source (agreed with the operator, 2026-10-13).

The vendor's own summary rows are read, never the detail rows: a sheet can carry stray rows copied from
another block below the product's table, and only the summary is the product's figure. IN STOCK counts
reserved slabs (reserved = available); sold = TOTAL - IN STOCK. A packing list that fails to load or has no
readable summary HOLDS the bundle's stock (never the site's possibly different figure in its place); a
bundle without one keeps the site's piece count.
"""

from __future__ import annotations

import polars as pl

from scrapers import sentas
from scrapers.sentas import SentasScraper
from stone_pipeline.adapters.sentas import ADAPTER

B4804 = """B4804 - GOLDEN EMPARADOR HONED,,,,GROSS SIZE,,,,,,NET SIZE,,,,,,,
,,,,,,,,,,,,,,,,,
BLOCK NO,BUNDLE NO.,LOT,MATERIAL,T,L,H,PCS,QUANTITY,WEIGHT,T,L,H,PCS,QUANTITY,WEIGHT,STATUS,BY
4804,1,B4804-1,GOLDEN EMPARADOR HONED,2,312,195,9,54.76,"3,011.58",2,300,190,9,51.30,"2,821.50",SOLD ,O.K
,,,,2,312,195,2,12.17,669.24,2,280,190,2,10.64,585.20,SOLD ,O.K
4804,2,B4804-2,GOLDEN EMPARADOR HONED,2,312,195,11,66.92,"3,680.82",2,307,190,11,64.16,"3,528.97",SOLD ,O.K
4804,3,B4804-3,GOLDEN EMPARADOR HONED,2,312,195,11,66.92,"3,680.82",2,307,190,11,64.16,"3,528.97",IN STOCK,
4804,4,B4804-4,GOLDEN EMPARADOR HONED,2,312,195,10,60.84,"3,346.20",2,307,190,10,58.33,"3,208.15",RESERVED,KN
4804,5,B4804-5,GOLDEN EMPARADOR HONED,2,312,195,11,66.92,"3,680.82",2,307,190,11,64.16,"3,528.97",IN STOCK,
TOTAL,,,,,,,54,328.54,"18,069.48",,,,54,312.44,"17,183.98",,
IN STOCK,,,,,,,32,194.69,"10,707.84",,,,54,312.44,"17,183.98",,
"""

# a product table followed by stray rows from ANOTHER block (all sold): only the summary is this product's
B5089_WITH_STRAY_ROWS = """B5089 - CALACATTA VIOLA HONED,,,,GROSS SIZE,,,,,,NET SIZE,,,,,,,
BLOCK NO,BUNDLE NO.,LOT,MATERIAL,T,L,H,PCS,QUANTITY,WEIGHT,T,L,H,PCS,QUANTITY,WEIGHT,STATUS,BY
5089,1,B5089-1,CALACATTA VIOLA HONED,2,284,181,26,66.83,"3,675.39",2,279,165,26,59.85,"3,291.50",IN STOCK,
5089,2,B5089-2,CALACATTA VIOLA HONED,2,284,181,26,66.83,"3,675.39",2,279,158,26,57.31,"3,151.86",IN STOCK,
6302,5,B6302-5,CALACATTA VIOLA HONED,2,317,156,11,54.40,"2,991.85",2,310,150,11,51.15,"2,813.25",SOLD,S.N
TOTAL,,,,,,,52,262.13,"14,417.26",,,,52,229.59,"12,627.40",,
IN STOCK,,,,,,,52,262.13,"14,417.26",,,,52,229.59,"12,627.40",,
"""

PIECES_LAYOUT = """BLOCK NO,BUNDLE NO.,LOT,MATERIAL,THICKNESS,LENGTH,WIDTH,PIECES,QUANTITY,WEIGHT,STATUS,BY
6111,1,B6111-1,AFYON WHITE,2,300,180,12,64.80,"3,560.00",IN STOCK,
TOTAL,,,,,,,12,64.80,"3,560.00",,
IN STOCK,,,,,,,12,64.80,"3,560.00",,
"""


class _Resp:
    def __init__(self, text: str):
        self.text = text


# -- reading the vendor's summary --------------------------------------------------------------------------

def test_stock_is_the_in_stock_summary_reserved_included_and_sold_is_the_rest():
    assert sentas._packing_counts(B4804) == (32, 22)


def test_stray_rows_from_another_block_never_count():
    assert sentas._packing_counts(B5089_WITH_STRAY_ROWS) == (52, 0)


def test_the_pieces_layout_is_read_too():
    assert sentas._packing_counts(PIECES_LAYOUT) == (12, 0)


def test_a_sheet_without_a_usable_summary_is_unreadable_not_guessed():
    no_summary = B4804.split("TOTAL")[0]
    assert sentas._packing_counts(no_summary) is None
    assert sentas._packing_counts("just,some,text\n1,2,3\n") is None
    in_stock_above_total = B4804.replace("IN STOCK,,,,,,,32", "IN STOCK,,,,,,,60")
    assert sentas._packing_counts(in_stock_above_total) is None


# -- finding the packing list on the product page ---------------------------------------------------------

PAGE = """
<a href="https://docs.google.com/spreadsheets/d/1XI-f_9A6fNiEglUa0FLej3lJlFYZ3HQn/edit?usp=drive_web&amp;rtpof=true" class="action-card-item card-drive"><div>Packing List</div><div>Google Drive Folder</div></a>
<a href="https://drive.google.com/drive/u/0/folders/1mqZ47Z0KmbZ3B9o1st9w0FCzdr8_h2QT" class="action-card-item card-drive"><div>Google Drive</div><div>Google Drive Folder</div></a>
"""


def test_the_packing_list_sheet_is_found_and_a_drive_file_link_is_not_a_sheet():
    assert sentas._packing_list_id(PAGE) == "1XI-f_9A6fNiEglUa0FLej3lJlFYZ3HQn"
    file_link = PAGE.replace("https://docs.google.com/spreadsheets/d/1XI-f_9A6fNiEglUa0FLej3lJlFYZ3HQn/edit",
                             "https://drive.google.com/file/d/18jqWpXgZ8R/view")
    assert sentas._packing_list_id(file_link) == ""
    assert sentas._packing_list_id("<html>no cards</html>") == ""


def test_the_scraper_records_the_packing_figures_and_a_failed_fetch(tmp_path, monkeypatch):
    s = SentasScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp(B4804))
    assert s._packing("sheet") == {"packing_status": "ok", "packing_available": 32, "packing_sold": 22}
    monkeypatch.setattr(s, "get", lambda url, **kw: _Resp("not,a,packing,list\n"))
    assert s._packing("sheet") == {"packing_status": "unreadable", "packing_available": "", "packing_sold": ""}

    def boom(url, **kw):
        raise RuntimeError("GET failed after 5 tries")
    monkeypatch.setattr(s, "get", boom)
    assert s._packing("sheet")["packing_status"] == "failed"
    assert [f["kind"] for f in s._failures] == ["packing_list"]
    assert s._packing("") == {"packing_status": "", "packing_available": "", "packing_sold": ""}


# -- the adapter: which figure is the stock -------------------------------------------------------------

def _adapt(**overrides):
    record = {"product_id": "2351", "url": "p", "variety": "Golden Emparador", "stone_type": "Marble",
              "finish": "Honed", "color": "", "thickness": "2 cm", "height_cm": "195 cm", "width_cm": "312 cm",
              "total_sqm": "328.54", "total_kg": "18069.48", "pieces": "54", "image_urls": "a",
              "format": "slab", "scrape_timestamp": "", "packing_status": "", "packing_available": "",
              "packing_sold": "", **overrides}
    return ADAPTER.adapt(pl.DataFrame([record]))[0]


def test_the_packing_list_is_the_stock_where_there_is_one():
    row = _adapt(packing_status="ok", packing_available="32", packing_sold="22")
    assert (row.raw_slab_count, row.raw_sold_count) == ("32", "22")
    assert row.raw_weight == str(round(18069.48 / 54, 3))           # weight per piece still from the page


def test_without_a_packing_list_the_site_count_is_the_stock_and_sold_is_unknown():
    row = _adapt()
    assert (row.raw_slab_count, row.raw_sold_count) == ("54", "")


def test_a_packing_list_that_failed_or_is_unreadable_holds_the_stock():
    assert _adapt(packing_status="failed").raw_slab_count == ""
    assert _adapt(packing_status="unreadable").raw_slab_count == ""
