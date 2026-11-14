"""The competitor price-sheet intake, on fixtures only (plan blokport_research/2026-11-11-market-prices-sheet-intake-plan.md,
Mas's ruling of 11-11): contract v2's `source` column, the layout file (exact header signature), the competitor named
from the file by its declared names, the price, currency and unit as the layout declares them, the size caps, the
idempotent sha record and the run mode. Nothing here reaches S3 or a site."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest
import yaml
from botocore.exceptions import ClientError

from market_prices import collect, config, intake
from market_prices.collect import _observation, stamp_for
from market_prices.layouts import LayoutError, Layouts, check_ids_apart, load_layouts
from market_prices.register import Register
from market_prices.schema import (
    ListingFacts, MalformedListing, Observation, PriceStatus, Provenance, SiteStatus, Source, columns,
)
from market_prices.sheet_files import FileRefused, read_tables
from market_prices.sheet_values import read_currency, read_price
from stone_pipeline.io.deliverables import MANIFEST_NAME

REPO = Path(__file__).resolve().parents[2]
RECEIVED = datetime(2026, 11, 11, 9, 15, 0, tzinfo=timezone.utc)
RUN_ID = "20261111T091500Z"
PREFIX = "dev/market_prices/blokport"


# --- fixtures ----------------------------------------------------------------------------------------------------------
def _competitors() -> list[dict]:
    return [
        {"competitor_id": "stonecorp_gb", "names": ["StoneCorp Ltd", "StoneCorp"], "seller_type": "wholesaler",
         "seller_country": "GB", "served_market": "GB", "site_domain": "stonecorp.example"},
        {"competitor_id": "rocca_it", "names": ["Rocca"], "seller_type": "merchant", "seller_country": "IT",
         "served_market": "GB", "site_domain": "rocca.example"},
    ]


def _layout(**overrides) -> dict:
    layout = {
        "layout_id": "stonecorp_csv", "header_row": 2, "headers": ["Code", "Product", "Finish", "Price", "Unit"],
        "competitor": {"cell": "A1"}, "listing_key": ["Code"], "listing_text": ["Product"], "price": "Price",
        "unit": "Unit", "number": {"decimal": ".", "thousands": ","},
        "statements": {"currency": "GBP", "vat_basis": "excl"}, "on_request_words": ["POA"],
        "currency_symbols": {"£": "GBP"},
    }
    return {**layout, **overrides}


def _layouts(*layouts: dict, competitors=None) -> Layouts:
    return Layouts.model_validate({"brand": "blokport", "competitors": competitors or _competitors(),
                                   "layouts": list(layouts or [_layout()])})


def _csv(*rows: list) -> bytes:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue().encode("utf-8")


STONECORP = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                 ["SC-1", "Nero Assoluto slab 2cm", "Polished", "1,234.50", "m2"],
                 ["SC-2", "Kashmir White slab", "Honed", "POA", "m2"])


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _file(data: bytes = STONECORP, key: str = f"{PREFIX}/intake/stonecorp.csv", version: str = "v1",
          received: datetime = RECEIVED) -> intake.IntakeFile:
    return intake.IntakeFile(key=key, version_id=version, last_modified=received, data=data)


def _absorb(data: bytes = STONECORP, layouts: Layouts | None = None, key: str = f"{PREFIX}/intake/stonecorp.csv"):
    return intake.absorb_file(_file(data, key), layouts or _layouts(), "bucket", "blokport")


def _status(result, site_id="stonecorp_gb") -> SiteStatus:
    return next(status for status in result.statuses if status.site_id == site_id)


# --- contract v2: the source column -------------------------------------------------------------------------------------
def test_the_contract_is_version_2_and_source_is_the_second_column():
    assert config.CONTRACT_VERSION == "2"
    assert columns(Observation)[:2] == ("contract_version", "source")
    assert columns(SiteStatus)[:2] == ("contract_version", "source")


def test_a_crawl_row_says_it_came_from_a_site():
    site = Register.model_validate({"brand": "blokport", "sites": [{
        "brand": "blokport", "site_id": "shop", "seller_name": "Shop", "seller_type": "merchant", "seller_country": "BE",
        "served_market": "NL", "platform": "woocommerce_store", "base_url": "https://shop.example",
        "terms": {"terms_url": "https://shop.example/terms", "checked_on": "2026-11-02", "summary": "None."},
        "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-08"}]}).sites[0]
    facts = ListingFacts(listing_key="1", source_url="https://shop.example/p/1", listing_text="Sill")
    assert _observation(facts, site, "blokport", stamp_for(RECEIVED), "woo/1").source is Source.site


def test_a_sheet_row_and_its_status_say_they_came_from_a_file():
    result = _absorb()
    assert {row.source for row in result.observations} == {Source.file}
    assert _status(result).source is Source.file


def test_the_contract_states_the_version_rule_and_the_source_column():
    text = (REPO / "market_prices" / "CONTRACT.md").read_text(encoding="utf-8")
    assert "version 2" in text and "| source |" in text
    assert "refuses a contract_version it does not know" in text


# --- the layout file ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("brand", ["blokport", "wudport"])
def test_the_committed_layout_files_validate(brand):
    assert load_layouts(brand).brand == brand


def test_a_layout_file_is_read_from_yaml_and_must_name_its_brand(tmp_path):
    document = {"brand": "blokport", "competitors": _competitors(), "layouts": [_layout()]}
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    assert [layout.layout_id for layout in load_layouts("blokport", tmp_path).layouts] == ["stonecorp_csv"]
    (tmp_path / "wudport.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(LayoutError, match="brand"):
        load_layouts("wudport", tmp_path)
    with pytest.raises(LayoutError, match="no sheet layouts"):
        load_layouts("limeport", tmp_path)


def test_two_layouts_with_one_header_signature_are_refused():
    twin = _layout(layout_id="stonecorp_copy", headers=["code", "PRODUCT", "Finish", "Price", "Unit"])
    with pytest.raises(ValueError, match="signature"):
        _layouts(_layout(), twin)


def test_layout_ids_are_unique():
    with pytest.raises(ValueError, match="layout_id"):
        _layouts(_layout(), _layout(headers=["Code", "Product", "Price", "Unit"]))


def test_headers_are_unique_within_a_layout():
    with pytest.raises(ValueError, match="header"):
        _layouts(_layout(headers=["Code", "Product", "Price", "Unit", "code"]))


@pytest.mark.parametrize("field, value", [("price", "Cost"), ("unit", "Per"), ("listing_key", ["Ref"]),
                                          ("listing_text", ["Name"]), ("currency", "Curr"),
                                          ("competitor", {"column": "Supplier"})])
def test_every_mapped_column_is_one_of_the_headers(field, value):
    with pytest.raises(ValueError, match="not a header"):
        _layouts(_layout(**{field: value}))


@pytest.mark.parametrize("source", [{}, {"cell": "A1", "column": "Code"}, {"cell": "1A"}, {"cell": "A0"},
                                    {"filename": "^prices_"}, {"filename": "(?P<name>["}])
def test_a_competitor_name_source_is_exactly_one_valid_cell_column_or_filename_pattern(source):
    with pytest.raises(ValueError):
        _layouts(_layout(competitor=source))


@pytest.mark.parametrize("number", [{"decimal": ",", "thousands": ","}, {"decimal": ";", "thousands": ""},
                                    {"decimal": ".", "thousands": "_"}])
def test_number_separators_are_declared_from_the_allowed_set_and_differ(number):
    with pytest.raises(ValueError):
        _layouts(_layout(number=number))


def test_currency_codes_in_the_layout_are_iso_4217():
    with pytest.raises(ValueError):
        _layouts(_layout(statements={"currency": "Pounds"}))
    with pytest.raises(ValueError):
        _layouts(_layout(currency_symbols={"£": "gbp"}))


def test_a_competitor_name_belongs_to_one_competitor_and_ids_are_unique():
    clash = [*_competitors(), {**_competitors()[1], "competitor_id": "rocca_gb", "names": ["stonecorp"]}]
    with pytest.raises(ValueError, match="name"):
        _layouts(competitors=clash)
    with pytest.raises(ValueError, match="competitor_id"):
        _layouts(competitors=[*_competitors(), _competitors()[0]])


def test_competitor_ids_and_site_ids_stay_apart():
    register = Register.model_validate({"brand": "blokport", "sites": [{
        "brand": "blokport", "site_id": "stonecorp_gb", "seller_name": "StoneCorp", "seller_type": "wholesaler", "platform":
        "woocommerce_store", "base_url": "https://stonecorp.example", "terms": {}, "decision": "pending"}]})
    with pytest.raises(LayoutError, match="stonecorp_gb"):
        check_ids_apart(_layouts(), register)
    check_ids_apart(_layouts(), Register.model_validate({"brand": "blokport", "sites": []}))


# --- the competitor, by its declared names -------------------------------------------------------------------------------
def test_a_competitor_is_resolved_by_a_declared_name_ignoring_case_and_spacing():
    layouts = _layouts()
    assert layouts.competitor_named("  stonecorp   LTD ").competitor_id == "stonecorp_gb"
    assert layouts.competitor_named("StoneCorp").competitor_id == "stonecorp_gb"
    assert layouts.competitor_named("Stone Corp") is None


def test_the_name_from_a_cell_names_every_row():
    result = _absorb()
    assert {(row.site_id, row.seller_name, row.seller_country) for row in result.observations} == \
        {("stonecorp_gb", "StoneCorp Ltd", "GB")}


def test_the_name_from_a_column_names_each_row():
    layout = _layout(layout_id="mixed", header_row=1, headers=["Supplier", "Code", "Product", "Price"],
                     competitor={"column": "Supplier"}, unit=None)
    data = _csv(["Supplier", "Code", "Product", "Price"], ["Rocca", "R1", "Carrara slab", "80"],
                ["StoneCorp", "S1", "Nero slab", "90"])
    result = _absorb(data, _layouts(layout))
    assert [(row.site_id, row.listing_key) for row in result.observations] == [("rocca_it", "R1"),
                                                                             ("stonecorp_gb", "S1")]
    assert [status.site_id for status in result.statuses] == ["rocca_it", "stonecorp_gb"]
    # identity columns stay in stated_specs as written (the manager, 13 Nov): the naming column too
    assert result.observations[0].stated_specs["Supplier"] == "Rocca"


def test_the_name_from_the_file_name():
    layout = _layout(header_row=1, competitor={"filename": r"^(?P<name>[A-Za-z]+)_prices\.csv$"})
    data = _csv(["Code", "Product", "Finish", "Price", "Unit"], ["R1", "Carrara slab", "Honed", "80", "m2"])
    result = _absorb(data, _layouts(layout), key=f"{PREFIX}/intake/Rocca_prices.csv")
    assert [row.site_id for row in result.observations] == ["rocca_it"]


def test_an_unknown_competitor_refuses_the_file():
    with pytest.raises(FileRefused, match="Granito Spa"):
        _absorb(STONECORP.replace(b"StoneCorp Ltd", b"Granito Spa"))
    layout = _layout(layout_id="mixed", header_row=1, headers=["Supplier", "Code", "Product", "Price"],
                     competitor={"column": "Supplier"}, unit=None)
    data = _csv(["Supplier", "Code", "Product", "Price"], ["Rocca", "R1", "Carrara", "80"], ["", "R2", "Bianco", "9"])
    with pytest.raises(FileRefused, match="row 3"):
        _absorb(data, _layouts(layout))


def test_a_file_name_the_pattern_does_not_match_refuses_the_file():
    layout = _layout(header_row=1, competitor={"filename": r"^(?P<name>[A-Za-z]+)_prices\.csv$"})
    data = _csv(["Code", "Product", "Finish", "Price", "Unit"], ["R1", "Carrara slab", "Honed", "80", "m2"])
    with pytest.raises(FileRefused, match="file name"):
        _absorb(data, _layouts(layout), key=f"{PREFIX}/intake/rocca-2026.csv")


# --- matching a file to a layout -----------------------------------------------------------------------------------------
def test_an_unknown_layout_is_refused_naming_the_file_and_its_headers():
    data = _csv(["StoneCorp Ltd"], ["Ref", "Item", "Cost"], ["1", "Slab", "9"])
    with pytest.raises(FileRefused) as refused:
        _absorb(data)
    assert "stonecorp.csv" in str(refused.value) and "Ref" in str(refused.value) and "Cost" in str(refused.value)


def test_the_header_row_is_the_declared_row_only():
    data = _csv(["Code", "Product", "Finish", "Price", "Unit"], ["SC-1", "Nero", "Polished", "9", "m2"])
    with pytest.raises(FileRefused):
        _absorb(data)                                           # the layout declares row 2, never searches for it


def test_trailing_empty_header_cells_do_not_change_the_signature():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit", "", ""],
                ["SC-1", "Nero", "Polished", "9", "m2"])
    assert [row.listing_key for row in _absorb(data).observations] == ["SC-1"]


def test_an_xlsx_sheet_is_read_by_the_layouts_sheet_name():
    layout = _layout(layout_id="stonecorp_xlsx", sheet="Price List")
    data = _xlsx({"Notes": [["ignore me"]],
                  "Price List": [["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                                 ["SC-1", "Nero slab", "Polished", 1234.5, "m2"]]})
    (row,) = _absorb(data, _layouts(layout), key=f"{PREFIX}/intake/stonecorp.xlsx").observations
    assert (row.price_amount, row.price_text, row.price_status) == ("1234.5", "1234.5", PriceStatus.priced)
    assert row.source_url == f"s3://bucket/{PREFIX}/intake/stonecorp.xlsx?versionId=v1#Price%20List!R3"


def test_a_csv_layout_does_not_match_an_xlsx_and_back():
    data = _xlsx({"Price List": [["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                                 ["SC-1", "Nero slab", "Polished", 9, "m2"]]})
    with pytest.raises(FileRefused):
        _absorb(data, key=f"{PREFIX}/intake/stonecorp.xlsx")          # the layout has no sheet: a csv layout
    with pytest.raises(FileRefused):
        _absorb(STONECORP, _layouts(_layout(sheet="Price List")))


# --- the rows ------------------------------------------------------------------------------------------------------------
def test_a_known_layout_gives_the_exact_rows():
    result = _absorb()
    assert result.run_id == RUN_ID and result.brand == "blokport"
    first = result.observations[0].model_dump(mode="json")
    assert first == {
        "contract_version": "2", "source": "file", "brand": "blokport", "site_id": "stonecorp_gb",
        "seller_name": "StoneCorp Ltd", "seller_type": "wholesaler", "seller_country": "GB", "served_market": "GB",
        "seller_is_our_supplier": False, "supplier_key": None, "listing_key": "SC-1", "source_url": f"s3://bucket/{PREFIX}/intake/stonecorp.csv?versionId=v1#R3",
        "listing_text": "Nero Assoluto slab 2cm", "stated_specs": {"Code": "SC-1", "Finish": "Polished"}, "grade_text": None,
        "price_text": "1,234.50", "price_amount": "1234.50", "currency": "GBP", "price_status": "priced",
        "vat_basis": "excl", "unit_text": "m2", "delivery_text": None, "delivery_included": "unknown",
        "min_qty_text": None, "availability_text": None,
        "provenance": {"vat_basis": "site_statement", "unit_text": "listing", "delivery_text": "unknown",
                       "delivery_included": "unknown", "grade_text": "unknown", "price_amount": "listing",
                       "currency": "site_statement", "min_qty_text": "unknown"},
        "observed_at": "2026-11-11T09:15:00Z", "run_id": RUN_ID, "collector_version": "sheet-intake/1:stonecorp_csv",
    }
    second = result.observations[1]
    assert (second.price_status, second.price_amount, second.price_text) == (PriceStatus.on_request, None, "POA")
    status = _status(result)
    assert (status.status.value, status.rows, status.malformed_listings, status.decision, status.terms_checked_on) == \
        ("ok", 2, 0, config.FILE_DECISION, None)


def test_a_row_without_text_or_key_is_malformed_and_the_others_still_count():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"], ["SC-1", "Nero", "", "9", "m2"],
                ["", "Keyless", "", "9", "m2"], ["SC-3", "", "Honed", "9", "m2"])
    result = _absorb(data)
    assert [row.listing_key for row in result.observations] == ["SC-1"]
    assert (_status(result).status.value, _status(result).malformed_listings) == ("partial", 2)


def test_a_composite_listing_key_joins_its_columns():
    layout = _layout(listing_key=["Product", "Finish"])
    assert [row.listing_key for row in _absorb(layouts=_layouts(layout)).observations] == \
        ["Kashmir White slab|Honed", "Nero Assoluto slab 2cm|Polished"]


def test_a_repeated_key_is_counted_and_the_first_is_kept():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"], ["SC-1", "First", "", "9", "m2"],
                ["SC-1", "Second", "", "8", "m2"])
    result = _absorb(data)
    assert [row.listing_text for row in result.observations] == ["First"]
    assert _status(result).duplicate_listings == 1


def test_blank_rows_are_skipped_and_not_counted():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"], ["", "", "", "", ""], [],
                ["SC-1", "Nero", "", "9", "m2"])
    result = _absorb(data)
    assert (_status(result).rows, _status(result).malformed_listings) == (1, 0)


def test_a_file_with_no_data_rows_is_refused():
    with pytest.raises(FileRefused, match="no data rows"):
        _absorb(_csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"], ["", "", "", "", ""]))


def test_a_row_with_cells_past_the_headers_is_malformed():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                ["SC-1", "Nero", "", "9", "m2", "stray"], ["SC-2", "Bianco", "", "9", "m2"])
    assert _status(_absorb(data)).malformed_listings == 1


def test_an_over_long_cell_makes_its_row_malformed(monkeypatch):
    # above every cell an ordinary row carries (its provenance JSON is about 210 characters), below the long one
    monkeypatch.setattr(config, "MAX_CELL_CHARS", 500)
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"], ["SC-1", "N" * 501, "", "9", "m2"],
                ["SC-2", "Bianco", "", "9", "m2"])
    assert [row.listing_key for row in _absorb(data).observations] == ["SC-2"]


def test_the_same_file_gives_the_same_rows():
    assert _absorb().observations == _absorb().observations


# --- price, currency and unit, as the layout declares ---------------------------------------------------------------------
@pytest.mark.parametrize("cell, amount, status, currency", [
    ("1,234.50", "1234.50", "priced", None), ("12", "12", "priced", None), ("12.5", "12.5", "priced", None),
    ("1,2,3", None, "none", None), ("1234,50", None, "none", None), ("10 - 20", None, "none", None),
    ("0", None, "none", None), ("0.00", None, "none", None), ("POA", None, "on_request", None),
    ("poa", None, "on_request", None), ("£12.50", "12.50", "priced", "GBP"), ("12.50 £", "12.50", "priced", "GBP"),
    ("€12.50", None, "none", None), ("1" * 19, None, "none", None), ("-5", None, "none", None),
])
def test_a_text_price_reads_only_through_the_declared_separators_and_symbols(cell, amount, status, currency):
    price = read_price(cell, _layouts().layouts[0])
    assert (price.text, price.amount, price.status.value, price.currency) == (cell, amount, status, currency)


def test_a_decimal_comma_layout_reads_its_own_numbers():
    layout = _layouts(_layout(number={"decimal": ",", "thousands": "."})).layouts[0]
    assert read_price("1.234,50", layout).amount == "1234.50"
    assert read_price("1,234.50", layout).amount is None


@pytest.mark.parametrize("cell, amount, text", [(12.5, "12.5", "12.5"), (3, "3", "3"), (0, None, "0"),
                                                (-4.0, None, "-4.0"), (float("nan"), None, None),
                                                (True, None, "TRUE"), (None, None, None), ("", None, None)])
def test_a_numeric_cell_is_its_exact_value(cell, amount, text):
    price = read_price(cell, _layouts().layouts[0])
    assert (price.amount, price.text) == (amount, text)
    assert price.status is (PriceStatus.priced if amount else PriceStatus.none)


def test_a_currency_cell_is_an_iso_code_as_written_or_a_declared_symbol():
    layout = _layouts().layouts[0]
    assert read_currency("GBP", layout) == "GBP"
    assert read_currency(" £ ", layout) == "GBP"
    assert read_currency("", layout) is None
    for undeclared in ("€", "gbp", "Pounds"):
        with pytest.raises(MalformedListing):
            read_currency(undeclared, layout)


def _with_currency_column() -> Layouts:
    return _layouts(_layout(headers=["Code", "Product", "Finish", "Price", "Unit", "Curr"], currency="Curr",
                            statements={"vat_basis": "excl"}))


def test_the_currency_column_wins_and_a_disagreeing_symbol_is_malformed():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit", "Curr"],
                ["SC-1", "Nero", "", "9", "m2", "EUR"], ["SC-2", "Bianco", "", "£9", "m2", "EUR"],
                ["SC-3", "Rosa", "", "£9", "m2", ""])
    result = _absorb(data, _with_currency_column())
    assert [(row.listing_key, row.currency, row.provenance["currency"].value) for row in result.observations] == \
        [("SC-1", "EUR", "listing"), ("SC-3", "GBP", "listing")]
    assert _status(result).malformed_listings == 1


def test_with_no_currency_said_the_currency_is_unknown():
    layout = _layout(statements={}, currency_symbols={})
    row = _absorb(layouts=_layouts(layout)).observations[0]
    assert (row.currency, row.provenance["currency"].value) == (None, "unknown")


def test_the_unit_is_kept_as_written_from_the_column_else_the_sheet_statement():
    data = _csv(["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                ["SC-1", "Nero", "", "9", "per  m²"], ["SC-2", "Bianco", "", "9", ""])
    rows = _absorb(data, _layouts(_layout(statements={"unit_text": "per tonne"}))).observations
    assert [(row.unit_text, row.provenance["unit_text"].value) for row in rows] == \
        [("per m²", "listing"), ("per tonne", "site_statement")]


# --- the file: formats and caps -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["prices.xls", "prices.ods", "prices.txt", "prices"])
def test_only_csv_and_xlsx_are_read(name):
    with pytest.raises(FileRefused, match=name):
        read_tables(name, b"anything")


def test_a_file_over_the_byte_cap_is_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_INTAKE_FILE_BYTES", 10)
    with pytest.raises(FileRefused, match="bytes"):
        read_tables("prices.csv", b"a,b,c\n1,2,3\n")


def test_a_csv_that_is_not_utf8_is_refused_and_a_bom_is_not():
    with pytest.raises(FileRefused, match="UTF-8"):
        read_tables("prices.csv", "Code,Prix\n1,12€\n".encode("cp1252"))
    (table,) = read_tables("prices.csv", "﻿Code,Price\n1,2\n".encode("utf-8"))
    assert table.rows[0] == ["Code", "Price"]


def test_a_sheet_over_the_row_or_column_cap_is_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_SHEET_ROWS", 3)
    with pytest.raises(FileRefused, match="rows"):
        read_tables("prices.csv", _csv(*[["x"]] * 4))
    with pytest.raises(FileRefused, match="rows"):
        read_tables("prices.xlsx", _xlsx({"S": [["x"]] * 4}))
    monkeypatch.setattr(config, "MAX_SHEET_ROWS", 100)
    monkeypatch.setattr(config, "MAX_SHEET_COLUMNS", 2)
    with pytest.raises(FileRefused, match="columns"):
        read_tables("prices.csv", _csv(["a", "b", "c"]))
    with pytest.raises(FileRefused, match="columns"):
        read_tables("prices.xlsx", _xlsx({"S": [["a", "b", "c"]]}))


def test_an_xlsx_that_expands_past_the_cap_is_refused_before_it_is_parsed(monkeypatch):
    monkeypatch.setattr(config, "MAX_XLSX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(FileRefused, match="expands"):
        read_tables("prices.xlsx", _xlsx({"S": [["a"]]}))


def test_an_xlsx_that_is_not_a_workbook_is_refused():
    with pytest.raises(FileRefused, match="workbook"):
        read_tables("prices.xlsx", b"not a zip")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("hello.txt", "no workbook in here")
    with pytest.raises(FileRefused, match="workbook"):
        read_tables("prices.xlsx", out.getvalue())


def test_a_formula_without_a_cached_value_makes_its_row_malformed():
    layout = _layout(layout_id="stonecorp_xlsx", sheet="Price List")
    data = _xlsx({"Price List": [["StoneCorp Ltd"], ["Code", "Product", "Finish", "Price", "Unit"],
                                 ["SC-1", "Nero slab", "", "=10*2", "m2"], ["SC-2", "Bianco", "", 9, "m2"]]})
    result = _absorb(data, _layouts(layout), key=f"{PREFIX}/intake/stonecorp.xlsx")
    assert [row.listing_key for row in result.observations] == ["SC-2"]
    assert _status(result).malformed_listings == 1


def _renamed_sheet(data: bytes, old: str, new: str) -> bytes:
    """The workbook with one sheet's title rewritten in workbook.xml: Excel never writes a title over 31 characters,
    so a longer one only comes from a hand-made file."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for part in source.infolist():
            body = source.read(part)
            if part.filename == "xl/workbook.xml":
                body = body.replace(f'name="{old}"'.encode(), f'name="{new}"'.encode())
            target.writestr(part, body)
    return out.getvalue()


def test_a_worksheet_title_over_excels_31_characters_refuses_the_file():
    data = _renamed_sheet(_xlsx({"S": [["a"]]}), "S", "Price" + " " * 5000 + "List")
    with pytest.raises(FileRefused, match="31"):
        read_tables("prices.xlsx", data)


def test_a_generated_cell_cannot_reach_the_cell_cap():
    """The cap guards the seller's text. A cell the intake builds from file metadata is bounded by S3's and Excel's
    own limits: a bucket name of 63 characters, a key and a version id of 1024 bytes each, a worksheet title of 31
    characters (anything longer is refused above) and the row cap. At every limit at once, percent-encoded at the
    worst rate (3 characters per byte of 4-byte UTF-8), the source_url is far under the cap, so no file name, sheet
    or version can make every row of a file malformed. The provenance cell is 8 fixed keys of fixed values."""
    file = intake.IntakeFile(key="\U0001F600" * 256, version_id="\U0001F600" * 256, last_modified=RECEIVED,
                             data=b"")
    url = intake._source_url("b" * 63, file, "\U0001F600" * 31, config.MAX_SHEET_ROWS - 1)
    assert len(url) < config.MAX_CELL_CHARS // 10
    longest = max((value.value for value in Provenance), key=len)
    fields = (*collect._SITE_STATEMENT_FIELDS, *collect._LISTING_FIELDS)
    provenance = json.dumps({name: longest for name in fields}, separators=(",", ":"))
    assert len(provenance) < config.MAX_CELL_CHARS // 100


def test_xlsx_xml_is_parsed_through_defusedxml():
    from openpyxl.xml import DEFUSEDXML
    assert DEFUSEDXML


# --- the sha record: each file once ---------------------------------------------------------------------------------------
class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class _S3:
    """A versioned bucket: list (paged), get, head and put, with the calls recorded."""

    def __init__(self, page_size: int = 1000):
        self.objects: dict[str, tuple[bytes, str, datetime]] = {}
        self.puts: list[str] = []
        self.gets: list[str] = []
        self.page_size = page_size
        self._count = 0

    def add(self, key: str, data: bytes, received: datetime = RECEIVED) -> None:
        self._count += 1
        self.objects[key] = (data, f"v{self._count}", received)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        answer = {"Contents": [{"Key": key, "Size": len(self.objects[key][0])} for key in page],
                  "IsTruncated": start + self.page_size < len(keys)}
        if answer["IsTruncated"]:
            answer["NextContinuationToken"] = str(start + self.page_size)
        return answer

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data, version, received = self.objects[Key]
        return {"Body": _Body(data), "VersionId": version, "LastModified": received}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"VersionId": self.objects[Key][1]}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        self.add(Key, Body if isinstance(Body, bytes) else Body.read())
        self.puts.append(Key)
        return {"VersionId": self.objects[Key][1]}


RECORD = f"{PREFIX}/{config.ABSORBED_FILE}"


def _intake(s3: _S3, tmp_path: Path, dry_run: bool = False, layouts: Layouts | None = None) -> intake.IntakeOutcome:
    return intake.run_intake(s3, "bucket", "blokport", layouts or _layouts(), tmp_path, dry_run)


def _record(s3: _S3) -> list[dict]:
    return list(csv.DictReader(io.StringIO(s3.objects[RECORD][0].decode("utf-8"))))


def test_a_file_is_absorbed_published_and_recorded_once(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    outcome = _intake(s3, tmp_path)
    assert outcome.absorbed == ((f"{PREFIX}/intake/stonecorp.csv", RUN_ID),) and outcome.refused == ()
    assert s3.puts == [f"{PREFIX}/{RUN_ID}/observations.csv", f"{PREFIX}/{RUN_ID}/site_status.csv",
                       f"{PREFIX}/{RUN_ID}/{MANIFEST_NAME}", f"{PREFIX}/latest.json", RECORD]
    assert _record(s3) == [{"sha256": hashlib.sha256(STONECORP).hexdigest(), "key": f"{PREFIX}/intake/stonecorp.csv",
                            "version_id": "v1", "layout_id": "stonecorp_csv", "run_id": RUN_ID}]
    s3.puts.clear()
    again = _intake(s3, tmp_path)
    assert (again.absorbed, again.skipped, s3.puts) == ((), (f"{PREFIX}/intake/stonecorp.csv",), [])


def test_a_renamed_copy_is_skipped_by_its_sha(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    _intake(s3, tmp_path)
    s3.add(f"{PREFIX}/intake/stonecorp copy.csv", STONECORP, datetime(2026, 11, 12, tzinfo=timezone.utc))
    assert _intake(s3, tmp_path).skipped == (f"{PREFIX}/intake/stonecorp copy.csv", f"{PREFIX}/intake/stonecorp.csv")


def test_the_same_bytes_twice_in_one_listing_are_absorbed_once(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/a.csv", STONECORP)
    s3.add(f"{PREFIX}/intake/b.csv", STONECORP)
    outcome = _intake(s3, tmp_path, dry_run=True)
    assert (len(outcome.absorbed), outcome.skipped) == (1, (f"{PREFIX}/intake/b.csv",))


def test_a_new_version_of_a_file_is_a_new_run(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    _intake(s3, tmp_path)
    later = datetime(2026, 11, 18, 8, 0, 0, tzinfo=timezone.utc)
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP.replace(b"1,234.50", b"1,199.00"), later)
    assert _intake(s3, tmp_path).absorbed == ((f"{PREFIX}/intake/stonecorp.csv", "20261118T080000Z"),)
    record = _record(s3)
    assert {row["run_id"] for row in record} == {RUN_ID, "20261118T080000Z"}
    assert [row["sha256"] for row in record] == sorted(row["sha256"] for row in record)


def test_a_crash_before_the_record_redoes_the_file_into_the_same_run_and_bytes(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    _intake(s3, tmp_path)
    observations = s3.objects[f"{PREFIX}/{RUN_ID}/observations.csv"][0]
    del s3.objects[RECORD]                                      # the run died after publishing, before its record
    s3.puts.clear()
    assert _intake(s3, tmp_path).absorbed == ((f"{PREFIX}/intake/stonecorp.csv", RUN_ID),)
    assert s3.objects[f"{PREFIX}/{RUN_ID}/observations.csv"][0] == observations
    assert f"{PREFIX}/{RUN_ID}/observations.csv" not in s3.puts and RECORD in s3.puts


def test_a_run_folder_held_by_another_run_refuses_the_file(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/{RUN_ID}/{MANIFEST_NAME}",
           json.dumps({"run_id": RUN_ID, "files": {"observations.csv": {"sha256": "0" * 64}}}).encode())
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    outcome = _intake(s3, tmp_path)
    assert outcome.absorbed == () and "already holds" in outcome.refused[0][1]
    assert s3.puts == []


def test_a_refused_file_does_not_stop_the_others(tmp_path):
    s3 = _S3(page_size=1)
    s3.add(f"{PREFIX}/intake/a_unknown.csv", _csv(["Who"], ["Ref", "Cost"], ["1", "2"]))
    s3.add(f"{PREFIX}/intake/b_old.xls", b"\xd0\xcf\x11\xe0")
    s3.add(f"{PREFIX}/intake/c_stonecorp.csv", STONECORP)
    outcome = _intake(s3, tmp_path)
    assert [key for key, _why in outcome.refused] == [f"{PREFIX}/intake/a_unknown.csv", f"{PREFIX}/intake/b_old.xls"]
    assert [key for key, _run in outcome.absorbed] == [f"{PREFIX}/intake/c_stonecorp.csv"]
    assert len(_record(s3)) == 1                                 # a refused file is not recorded: a new layout reads it


def test_a_file_over_the_cap_is_refused_from_the_listing_without_reading_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_INTAKE_FILE_BYTES", 10)
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/big.csv", STONECORP)
    outcome = _intake(s3, tmp_path)
    assert "bytes" in outcome.refused[0][1] and f"{PREFIX}/intake/big.csv" not in s3.gets


def test_folder_markers_and_other_brands_are_not_read(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/", b"")
    s3.add("dev/market_prices/wudport/intake/stonecorp.csv", STONECORP)
    outcome = _intake(s3, tmp_path)
    assert (outcome.absorbed, outcome.skipped, outcome.refused) == ((), (), ())


def test_the_dry_run_writes_the_run_folder_and_publishes_and_records_nothing(tmp_path):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/stonecorp.csv", STONECORP)
    outcome = _intake(s3, tmp_path, dry_run=True)
    assert outcome.absorbed == ((f"{PREFIX}/intake/stonecorp.csv", RUN_ID),) and s3.puts == []
    assert (tmp_path / "blokport" / RUN_ID / config.OBSERVATIONS_FILE).exists()


def test_a_record_that_does_not_read_fails_loud(tmp_path):
    s3 = _S3()
    s3.add(RECORD, b"sha,key\nabc,x\n")
    with pytest.raises(intake.IntakeError, match=config.ABSORBED_FILE):
        _intake(s3, tmp_path)


def test_the_entry_exits_non_zero_when_a_file_was_refused(tmp_path, monkeypatch, capsys):
    s3 = _S3()
    s3.add(f"{PREFIX}/intake/unknown.csv", _csv(["Who"], ["Ref", "Cost"], ["1", "2"]))
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "blokport")
    monkeypatch.setattr(intake, "load_layouts", lambda brand: _layouts())
    monkeypatch.setattr(intake, "load_register", lambda brand: Register.model_validate({"brand": brand, "sites": []}))
    monkeypatch.setattr(intake, "_s3_client", lambda: s3)
    monkeypatch.setattr(intake, "_runs_root", lambda: tmp_path)
    monkeypatch.setattr(intake, "_dry_run", lambda: False)
    with pytest.raises(SystemExit) as stopped:
        intake.main()
    assert stopped.value.code == 1 and "refused: dev/market_prices/blokport/intake/unknown.csv" in capsys.readouterr().out


def test_the_entry_absorbs_the_run_inputs_brand_not_the_deployments(tmp_path, monkeypatch):
    # this deployment is blokport's (the dev default); the run input names wudport, so wudport's layouts and register
    # are read and wudport's intake prefix is worked: the one shared runner
    read: list[tuple[str, str]] = []
    worked: list[str] = []

    def layouts(brand: str) -> Layouts:
        read.append(("layouts", brand))
        return _layouts()

    def register(brand: str) -> Register:
        read.append(("register", brand))
        return Register.model_validate({"brand": brand, "sites": []})

    def work(client, bucket, brand, layouts, root, dry_run) -> intake.IntakeOutcome:
        worked.append(brand)
        return intake.IntakeOutcome(absorbed=(), skipped=(), refused=())
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "wudport")
    monkeypatch.setattr(intake, "load_layouts", layouts)
    monkeypatch.setattr(intake, "load_register", register)
    monkeypatch.setattr(intake, "run_intake", work)
    monkeypatch.setattr(intake, "_s3_client", lambda: _S3())
    monkeypatch.setattr(intake, "_dry_run", lambda: False)
    intake.main()
    assert read == [("layouts", "wudport"), ("register", "wudport")] and worked == ["wudport"]


def test_the_entry_without_the_brand_input_stops_before_any_file_or_bucket_is_read(monkeypatch):
    monkeypatch.setattr(intake, "load_layouts", lambda brand: pytest.fail(f"the {brand} layouts were read"))
    monkeypatch.setattr(intake, "_s3_client", lambda: pytest.fail("a bucket client was built"))
    with pytest.raises(SystemExit, match="SCRAPER_MARKET_PRICES_BRAND"):
        intake.main()


def test_the_entry_refuses_an_unknown_run_brand_before_any_bucket_is_read(monkeypatch):
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "no_such_brand")
    monkeypatch.setattr(intake, "_s3_client", lambda: pytest.fail("a bucket client was built"))
    with pytest.raises(LayoutError, match="no sheet layouts for brand 'no_such_brand'"):
        intake.main()


# --- RUN_MODE ----------------------------------------------------------------------------------------------------------
def test_run_mode_market_prices_intake_runs_the_intake_entry(tmp_path):
    fake = tmp_path / "python"
    fake.write_text('#!/bin/sh\necho "python $*"\n')
    fake.chmod(0o755)
    done = subprocess.run(["bash", str(REPO / "deploy" / "run_pipeline.sh")], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", "RUN_MODE": "market-prices-intake"}, timeout=30)
    assert done.returncode == 0 and done.stdout.strip().splitlines()[-1] == "python -m market_prices.intake"


def test_the_intake_imports_nothing_from_the_produce():
    text = (REPO / "market_prices" / "intake.py").read_text(encoding="utf-8")
    assert not any(f"stone_pipeline.{name}" in text for name in ("stages", "matching", "ledger", "emit"))


# --- one sheet, several competitors, each priced from its own stated column (W7, the manager 13 Nov) -----------------
_TWO_LISTS = ["Code", "Product", "List", "List -20%", "Net", "Source"]


def _two_lists(**overrides) -> dict:
    """A sheet naming its competitor per row (the Source column), each competitor's price in its own column."""
    layout = {**_layout(layout_id="two_lists", header_row=1, headers=_TWO_LISTS, competitor={"column": "Source"},
                        unit=None, statements={"currency": "EUR", "vat_basis": "excl", "unit_text": "m²"},
                        currency_symbols={}),
              "price_by_competitor": {"stonecorp_gb": "List", "rocca_it": "Net"}}
    layout.pop("price")
    return {**layout, **overrides}


TWO_LISTS = _csv(_TWO_LISTS, ["SC-1", "Nero slab", "100.00", "80.00", "77.00", "StoneCorp Ltd"],
                 ["R-1", "Rosso slab", "", "", "55.50", "Rocca"])


def test_each_row_takes_its_own_competitors_stated_price_column():
    result = _absorb(TWO_LISTS, _layouts(_two_lists()), key=f"{PREFIX}/intake/two_lists.csv")
    rows = {row.site_id: row for row in result.observations}
    assert (rows["stonecorp_gb"].price_amount, rows["rocca_it"].price_amount) == ("100.00", "55.50")
    # every other stated column rides in stated_specs as written, another competitor's price column included
    assert rows["stonecorp_gb"].stated_specs == {"Code": "SC-1", "List -20%": "80.00", "Net": "77.00",
                                                 "Source": "StoneCorp Ltd"}
    assert "Net" not in rows["rocca_it"].stated_specs


@pytest.mark.parametrize("layout, message", [
    (_two_lists(price="List"), "not both"),
    ({k: v for k, v in _two_lists().items() if k != "price_by_competitor"}, "names no price column"),
    (_two_lists(price_by_competitor={"stonecorp_gb": "List", "nobody": "Net"}), "not a declared competitor"),
    (_two_lists(price_by_competitor={"stonecorp_gb": "List", "rocca_it": "Missing"}), "is not a header"),
])
def test_a_layout_names_one_price_column_per_competitor_or_is_refused(layout, message):
    with pytest.raises(ValueError, match=message):
        _layouts(layout)


def test_the_identity_columns_stay_in_stated_specs_as_written():
    # the naming column and every listing_key part are identity, not observation fields (the manager, 13 Nov): kept
    # as written, so a consumer reads thickness or finish from stated_specs and never parses listing_key
    result = _absorb(TWO_LISTS, _layouts(_two_lists()), key=f"{PREFIX}/intake/two_lists.csv")
    rows = {row.site_id: row for row in result.observations}
    assert (rows["stonecorp_gb"].stated_specs["Source"], rows["rocca_it"].stated_specs["Source"]) == (
        "StoneCorp Ltd", "Rocca")


def test_a_row_naming_a_competitor_without_a_price_column_refuses_the_file():
    layouts = _layouts(_two_lists(price_by_competitor={"stonecorp_gb": "List"}))
    with pytest.raises(FileRefused, match="rocca_it"):
        _absorb(TWO_LISTS, layouts, key=f"{PREFIX}/intake/two_lists.csv")


# --- the owner's competitor price sheets (W7: M22/P4's mapping, the owner's answers of 13 Nov via Mas) ---------------
# Each tab of the owner's workbook All_Competitor_Stone_Prices.xlsx (2026-11-11) arrives as its own file, the tab
# copied unchanged; these are its exact header rows. Every row names its competitor by the source document it was
# transcribed from ('Source file'), which is what tells Cereser's main list from its Listino Carrara.
_PRICE_M2 = "(EUR/m² ex VAT)"
_OWNER_TABS = {
    "Brachot Prices": [
        "Material", "Grade / selection", "Brachot product name", "Stone type", "Origin", "Origin code",
        "Thickness (cm)", "Finish", "Treatment / calibration", "Dimensions", f"Price {_PRICE_M2}",
        f"Base list price {_PRICE_M2}", "Adjustment (%)", "Original pricing conditions", "Brachot price category",
        "Catalog page", "PDF page", "Price status", "Catalog section", "Catalog edition / effective date", "Notes",
        "Source file", "Sales terms"],
    "Tuytelaers Prices": [
        "Material", "Grade / selection", "Tuytelaers product name", "Stone type", "Origin", "Origin code",
        "Thickness (cm)", "Finish", "Dimensions", f"Price {_PRICE_M2}", f"Base list price {_PRICE_M2}",
        "Adjustment (%)", "Original pricing conditions", "Catalog section", "Catalog page", "PDF page", "Price status",
        "New in 2026 catalog", "Notes", "Catalog edition / effective date", "Source file", "Sales terms"],
    "Cereser Prices": [
        "Material", "Grade / selection", "Cereser product name (source)", "Stone type", "Source category", "Origin",
        "Origin (source)", "Thickness (cm)", "Finish", "Dimensions", "Block / lot", "Slab selection / pricing basis",
        "List price (EUR/m²)", "Price -20% (EUR/m²)", "Carrara net price (EUR/m²)",
        "Price used in master (EUR/m²)", "Discount (%)", "Price status", "Source file", "Source row", "List date",
        "Lot date / source note", "Vendor notes"],
}


def _owner_rows(tab: str, *rows: dict) -> dict[str, Observation]:
    """The committed blokport layouts reading one owner tab holding `rows`, by listing_key."""
    headers = _OWNER_TABS[tab]
    data = _xlsx({tab: [headers, *([row.get(header) for header in headers] for row in rows)]})
    key = f"{PREFIX}/intake/owner_{tab.split()[0].lower()}.xlsx"
    result = intake.absorb_file(_file(data, key), load_layouts("blokport"), "bucket", "blokport")
    return {row.listing_key: row for row in result.observations}


def test_the_owner_competitors_are_declared_from_the_owners_answers():
    competitors = load_layouts("blokport").competitors
    assert {c.competitor_id: (c.site_domain, c.seller_country, c.served_market, c.seller_type.value, c.supplier_key)
            for c in competitors} == {
        "brachot": ("brachot.com", "BE", "EU", "wholesaler", None),
        "tuytelaers": ("tuytelaers.be", "BE", "EU", "wholesaler", None),
        "cereser": ("ceresermarmi.com", "IT", "EU", "wholesaler", None),
        "cereser_carrara": ("ceresermarmi.com", "IT", "EU", "wholesaler", None),
    }


def test_a_brachot_row_is_its_own_sizes_price_per_m2_ex_vat_with_identity_kept():
    rows = _owner_rows("Brachot Prices", {
        "Material": "Belgische Blauwe Hardsteen", "Brachot product name": "Belgische Blauwe Hardsteen Courant",
        "Stone type": "blauwe hardsteen", "Thickness (cm)": 2, "Finish": "Gezaagd", "Dimensions": "Standard sizes",
        f"Price {_PRICE_M2}": 76.9, f"Base list price {_PRICE_M2}": 70, "Adjustment (%)": 10, "Catalog page": 10,
        "PDF page": 6, "Source file": "Platentarief_050825_BE_NL_spreads(7).pdf"})
    row = rows["Belgische Blauwe Hardsteen Courant|2|Gezaagd|Standard sizes|10"]
    assert (row.site_id, row.seller_name, row.listing_text) == ("brachot", "Brachot",
                                                                "Belgische Blauwe Hardsteen Courant")
    assert (row.price_amount, row.currency, row.unit_text, row.vat_basis.value) == ("76.9", "EUR", "m²", "excl")
    kept = ("Thickness (cm)", "Finish", "Dimensions", "Stone type", f"Base list price {_PRICE_M2}", "Catalog page",
            "PDF page", "Source file")
    assert {key: row.stated_specs[key] for key in kept} == {
        "Thickness (cm)": "2", "Finish": "Gezaagd", "Dimensions": "Standard sizes", "Stone type": "blauwe hardsteen",
        f"Base list price {_PRICE_M2}": "70", "Catalog page": "10", "PDF page": "6",
        "Source file": "Platentarief_050825_BE_NL_spreads(7).pdf"}
    assert f"Price {_PRICE_M2}" not in row.stated_specs


def test_a_tuytelaers_row_keys_on_its_catalog_section():
    # one stone, thickness and finish listed in two sections at two prices: two listings, never one
    same = {"Tuytelaers product name": "Star Galaxy", "Thickness (cm)": 3, "Finish": "gepolijst",
            "Dimensions": "Not specified / standard", "Source file": "prijslijst-platen-2026(3).pdf"}
    rows = _owner_rows("Tuytelaers Prices", {**same, "Catalog section": "Natuursteen", f"Price {_PRICE_M2}": 140},
                       {**same, "Catalog section": "Blokzaag", f"Price {_PRICE_M2}": 187})
    stem = "Star Galaxy|3|gepolijst|Not specified / standard"
    assert {key: (row.site_id, row.price_amount) for key, row in rows.items()} == {
        f"{stem}|Natuursteen": ("tuytelaers", "140"), f"{stem}|Blokzaag": ("tuytelaers", "187")}


def test_each_cereser_list_is_its_own_competitor_priced_from_its_own_column():
    rows = _owner_rows(
        "Cereser Prices",
        {"Cereser product name (source)": "BLACK ANTIQUE *", "Thickness (cm)": 2, "List price (EUR/m²)": 88.5,
         "Price -20% (EUR/m²)": 70.8, "Price used in master (EUR/m²)": 70.8,
         "Source file": "Italian supplier list(1).xlsx", "Source row": 2},
        {"Cereser product name (source)": "BIANCO CARRARA C", "Thickness (cm)": 2, "Finish": "Lucido",
         "Carrara net price (EUR/m²)": 60, "Price used in master (EUR/m²)": 60,
         "Source file": "LISTINO CARRARA -(2).xlsx", "Source row": 7},
        {"Cereser product name (source)": "AZUL MACAUBAS", "Thickness (cm)": 3, "Price status": "Unpriced",
         "Source file": "Italian supplier list(1).xlsx", "Source row": 9})
    main, carrara, unpriced = rows["BLACK ANTIQUE *|2|2"], rows["BIANCO CARRARA C|2|7"], rows["AZUL MACAUBAS|3|9"]
    assert (main.site_id, main.price_amount, main.vat_basis.value) == ("cereser", "88.5", "excl")
    assert (carrara.site_id, carrara.price_amount, carrara.vat_basis.value) == ("cereser_carrara", "60", "excl")
    assert (main.stated_specs["Price -20% (EUR/m²)"], main.stated_specs["Source file"]) == (
        "70.8", "Italian supplier list(1).xlsx")
    assert carrara.stated_specs["Finish"] == "Lucido"
    # an unpriced row is kept as stated, never priced from another column
    assert (unpriced.price_amount, unpriced.price_status) == (None, PriceStatus.none)
