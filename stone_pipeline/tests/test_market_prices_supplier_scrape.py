"""A supplier's prices read from the product scraper's own fetch (platform supplier_scrape): one fetch, one transport,
on fixtures only. The register row, the facts read from the scraper's rows (currency and unit as stated, never
converted), the scrape's own statement of whether it read prices, the produce's generic selection of which suppliers a
scrape feeds, and the run stamped with the scrape's own time."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from market_prices import supplier
from market_prices.collect import collect_run, collect_supplier_run
from market_prices.collectors.supplier_scrape import ScrapeFolder, read_scrape
from market_prices.fetching import Response
from market_prices.register import Register

POL = "https://polonine.slabware.com"
SCRAPE = {"key": "product_id", "url": "detail_url", "price": "price1",
          "text": ["material", "stone_type", "thickness", "finish"],
          "specs": ["material", "stone_type", "thickness", "finish", "price2", "area_sqmt", "slab_count"]}


def _site(site_id="polonine", supplier_key="polonine", base_url=POL, **overrides) -> dict:
    site = {
        "brand": "blokport", "site_id": site_id, "seller_name": "Polonine", "seller_type": "wholesaler",
        "seller_country": "IT", "served_market": "NL", "platform": "supplier_scrape", "base_url": base_url,
        "supplier_key": supplier_key, "scrape": SCRAPE,
        "terms": {"terms_url": f"{base_url}/Terms-conditions.aspx", "checked_on": "2026-11-13",
                  "summary": "Placeholder text, no clause; the owner's own customer login."},
        "decision": "approved", "decided_by": "owner (via Mas)", "decided_on": "2026-11-13",
    }
    return {**site, **overrides}


def _register(*sites: dict) -> Register:
    return Register.model_validate({"brand": "blokport", "sites": list(sites or [_site()])})


def _row(pid="101", price1="US$ 59.00<span style='font-size: smaller;'>/Sq.Mt.</span>",
         price2="US$ 5.48<span style='font-size: smaller;'>/Sq.Ft.</span>", **overrides) -> dict:
    row = {"product_id": pid, "material": "ALPINE", "stone_type": "Dolomite Marble", "thickness": "2CM",
           "finish": "Polished", "price1": price1, "price2": price2, "currency": "", "area_sqmt": "55.160",
           "slab_count": "6", "detail_url": f"{POL}/Product-Details.aspx?ID={pid}"}
    return {**row, **overrides}


def _scrape(*rows: dict, prices="ok", source="polonine", timestamp="20261114_120000") -> ScrapeFolder:
    return ScrapeFolder(source=source, timestamp=timestamp, rows=tuple(rows or [_row()]), prices=prices)


def _status(result, site_id="polonine"):
    return next(status for status in result.statuses if status.site_id == site_id)


# --- the facts, as stated --------------------------------------------------------------------------------------------
def test_a_supplier_price_is_read_as_stated_and_labelled_the_suppliers():
    result = collect_supplier_run(_register(), _scrape())
    row = result.observations[0]
    assert (row.price_amount, row.currency, row.price_status.value) == ("59.00", "USD", "priced")
    assert row.price_text == "US$ 59.00 /Sq.Mt."
    assert (row.unit_text, row.provenance["unit_text"].value) == ("Sq.Mt.", "listing")
    assert (row.seller_is_our_supplier, row.supplier_key, row.listing_key) == (True, "polonine", "101")
    assert (row.vat_basis.value, row.provenance["vat_basis"].value) == ("unknown", "unknown")
    assert row.stated_specs["price2"] == "US$ 5.48 /Sq.Ft."
    assert row.listing_text == "ALPINE | Dolomite Marble | 2CM | Polished"
    assert row.source_url == f"{POL}/Product-Details.aspx?ID=101"
    assert (row.run_id, row.observed_at) == ("20261114T120000Z", "2026-11-14T12:00:00Z")
    assert row.collector_version == "supplier_scrape-1"
    assert _status(result).status.value == "ok"


def test_call_for_price_is_on_request_with_its_words_kept():
    row = collect_supplier_run(_register(), _scrape(_row(price1="<span class='i339'>Call for Price</span>",
                                                         price2=""))).observations[0]
    assert (row.price_status.value, row.price_amount, row.currency, row.price_text) == (
        "on_request", None, None, "Call for Price")


def test_a_grouped_amount_is_read_exactly():
    row = collect_supplier_run(_register(), _scrape(_row(price1="US$ 1,234.50/Sq.Mt."))).observations[0]
    assert row.price_amount == "1234.50"


def test_an_undeclared_currency_symbol_is_never_guessed_and_is_counted():
    result = collect_supplier_run(_register(), _scrape(_row(price1="R$ 59.00/Sq.Mt.")))
    row = result.observations[0]
    assert (row.price_status.value, row.price_amount, row.currency, row.price_text) == (
        "none", None, None, "R$ 59.00/Sq.Mt.")
    assert "price in a currency symbol not declared x1" in _status(result).detail


@pytest.mark.parametrize("price1", ["US$ about 59", "US$ 0.00/Sq.Mt.", "US$ 59,00/Sq.Mt.", "59.00"])
def test_a_price_that_is_not_one_exact_stated_amount_is_no_price_and_its_text_kept(price1):
    row = collect_supplier_run(_register(), _scrape(_row(price1=price1))).observations[0]
    assert (row.price_status.value, row.price_amount) == ("none", None)
    assert row.price_text == price1


def test_an_empty_price_is_no_price_and_no_text():
    row = collect_supplier_run(_register(), _scrape(_row(price1=""))).observations[0]
    assert (row.price_status.value, row.price_text, row.price_amount) == ("none", None, None)


@pytest.mark.parametrize("change, reason", [
    ({"product_id": ""}, "listing has no key"),
    ({"detail_url": "https://elsewhere.example/p/1"}, "listing url is not on the site"),
    ({"material": "", "stone_type": "", "thickness": "", "finish": ""}, "listing has no text"),
])
def test_an_unreadable_scrape_row_is_malformed_with_its_reason(change, reason):
    result = collect_supplier_run(_register(), _scrape(_row("1"), _row("2", **change)))
    status = _status(result)
    assert (status.status.value, status.rows, status.malformed_listings) == ("partial", 1, 1)
    assert f"{reason} x1" in status.detail


# --- the scrape's own statement of whether it read prices -----------------------------------------------------------
def test_a_scrape_that_read_no_prices_is_reported_with_its_reason_never_delivered_unpriced():
    result = collect_supplier_run(_register(), _scrape(prices="login refused: Invalid user"))
    status = _status(result)
    assert (status.status.value, status.rows, result.observations) == ("unavailable_error", 0, ())
    assert "login refused: Invalid user" in status.detail


def test_a_scrape_that_states_nothing_about_prices_is_reported_not_trusted():
    status = _status(collect_supplier_run(_register(), _scrape(prices=None)))
    assert (status.status.value, status.rows) == ("unavailable_error", 0)
    assert "states no prices" in status.detail


def test_an_unapproved_supplier_row_is_registered_but_never_read():
    site = _site(decision="pending")
    for key in ("seller_country", "served_market", "scrape"):
        del site[key]
    status = _status(collect_supplier_run(_register(site), _scrape()))
    assert (status.status.value, status.rows) == ("disabled_terms", 0)


# --- the register row ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change", [
    {"supplier_key": None},                                                      # it reads its supplier's scrape
    {"discovery": {"sitemaps": ["/s.xml"], "product_path": "^/p/"}},             # it fetches nothing itself
    {"query": {"x": "1"}},
    {"scrape": {**SCRAPE, "text": []}},                                          # its listings need a text
    {"scrape": {**SCRAPE, "surprise": "x"}},
])
def test_a_bad_supplier_row_is_refused(change):
    with pytest.raises(ValidationError):
        _register(_site(**change))


def test_an_approved_supplier_row_without_its_columns_is_refused():
    site = _site()
    del site["scrape"]
    with pytest.raises(ValidationError, match="scrape"):
        _register(site)


def test_a_fetching_site_may_not_carry_scrape_columns():
    with pytest.raises(ValidationError, match="scrape"):
        _register(_site(platform="woocommerce_store", supplier_key=None))


# --- one fetch: the fetching run never reads a supplier row ----------------------------------------------------------
class _Sites:
    def __init__(self):
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        raise AssertionError(f"no request expected, got {url}")


def test_the_fetching_run_leaves_supplier_rows_to_their_suppliers_scrape(monkeypatch):
    sites = _Sites()
    from datetime import datetime, timezone
    result = collect_run(_register(), sites, datetime(2026, 11, 14, tzinfo=timezone.utc), lambda _s: None)
    assert (result.statuses, result.observations, sites.requested) == ((), (), [])


# --- the produce's selection: generic, never one supplier named -------------------------------------------------------
def _two_suppliers() -> Register:
    return _register(_site(), _site("other_supplier", supplier_key="varsha", base_url="https://varsha.example"),
                     {**_site("shop", supplier_key=None, base_url="https://shop.example"),
                      "platform": "woocommerce_store", "scrape": None})


def test_the_suppliers_a_scrape_feeds_are_the_registers_supplier_rows_for_its_sources():
    register = _two_suppliers()
    assert supplier.supplier_sources(register, None) == ["polonine", "varsha"]
    assert supplier.supplier_sources(register, ["varsha", "zucchi"]) == ["varsha"]


def test_a_produce_scoped_to_other_sources_feeds_no_supplier():
    assert supplier.supplier_sources(_two_suppliers(), ["zucchi"]) == []


# --- the scrape folder: this produce's own, complete ---------------------------------------------------------------
def _folder(root: Path, source: str, stamp: str, complete=True, prices="ok", rows=(), **marker) -> Path:
    folder = root / source / stamp
    folder.mkdir(parents=True)
    with (folder / "products.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_row()))
        writer.writeheader()
        writer.writerows(rows or [_row()])
    facts = {"rows": len(rows or [1]), "complete": complete, "timestamp": stamp, **marker}
    if prices is not None:
        facts["prices"] = prices
    (folder / "scrape_complete.json").write_text(json.dumps(facts), encoding="utf-8")
    return folder


def test_the_scrape_read_is_this_produces_newest_complete_one(tmp_path):
    _folder(tmp_path, "polonine", "20261114_100000")                     # before this produce
    _folder(tmp_path, "polonine", "20261114_130000", complete=False)     # this produce, but not complete
    newest = _folder(tmp_path, "polonine", "20261114_120000")
    assert supplier.newest_scrape(tmp_path / "polonine", "20261114_110000") == newest
    assert supplier.newest_scrape(tmp_path / "polonine", "20261114_140000") is None
    assert supplier.newest_scrape(tmp_path / "absent", "20261114_110000") is None


def test_a_scrape_folder_reads_into_its_rows_its_time_and_its_price_statement(tmp_path):
    scrape = read_scrape(_folder(tmp_path, "polonine", "20261114_120000", prices="login refused: x"))
    assert (scrape.source, scrape.timestamp, scrape.prices, scrape.rows[0]["product_id"]) == (
        "polonine", "20261114_120000", "login refused: x", "101")
    assert read_scrape(_folder(tmp_path, "polonine", "20261114_130000", prices=None)).prices is None


def test_emit_delivers_one_run_per_supplier_scraped_and_none_for_a_supplier_not_scraped(tmp_path):
    _folder(tmp_path, "polonine", "20261114_120000")
    delivered = []
    outcomes = supplier.emit(_two_suppliers(), None, tmp_path, "20261114_110000", delivered.append)
    assert [result.run_id for result in delivered] == ["20261114T120000Z"]
    assert [status.site_id for status in delivered[0].statuses] == ["polonine"]
    assert len(outcomes) == 1
