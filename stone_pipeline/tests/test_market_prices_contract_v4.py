"""market_prices contract v4 (CONTRACT.md 36e4d4f0, read by M22 and P4): register.json in every run folder, so the
consumer upserts its sources from the scraper's own register; and Mas's sale rule, the regular price columns. The
register rows carry the owner's terms (price_basis, a net discount, a stated VAT rate), each with who stated it, never
defaulted or inferred. Fixtures only."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from market_prices import config
from market_prices.collect import collect_run, write_run
from market_prices.layouts import Layouts
from market_prices.publish import publish_run
from market_prices.register import Register
from stone_pipeline.tests.test_market_prices import (
    MOMENT, _S3, _listing, _no_pause, _page, _register, _shop, _site,
)

REGISTER_KEYS = {"brand", "source_key", "source", "seller_name", "seller_type", "country_iso_2", "served_market",
                 "price_basis", "net_discount", "net_discount_stated_by", "vat_rate", "vat_rate_stated_by",
                 "seller_is_our_supplier", "supplier_key", "decided_by", "decided_on"}


def _competitor(**overrides) -> dict:
    competitor = {"competitor_id": "rocca_it", "names": ["Rocca"], "seller_type": "wholesaler", "seller_country": "IT",
                  "served_market": "EU", "site_domain": "rocca.example", "price_basis": "net", "net_discount": "0.2",
                  "net_discount_stated_by": "owner", "decided_by": "owner (supplied workbook) via Mas",
                  "decided_on": "2026-11-13"}
    return {**competitor, **overrides}


def _layouts(*competitors: dict) -> Layouts:
    return Layouts.model_validate({"brand": "blokport", "competitors": list(competitors or [_competitor()]),
                                   "layouts": []})


# --- the register's terms: declared, never defaulted -----------------------------------------------------------------
@pytest.mark.parametrize("change, message", [
    ({"price_basis": None}, "price_basis"),                                           # an approved row declares it
    ({"price_basis": "net"}, "net_discount"),                                         # net needs its discount
    ({"net_discount": "0.2", "net_discount_stated_by": "owner"}, "net_discount"),     # list takes none
    ({"price_basis": "net", "net_discount": "1", "net_discount_stated_by": "owner"}, "net_discount"),   # [0, 1)
    ({"price_basis": "net", "net_discount": "0.2"}, "net_discount_stated_by"),        # who stated it
    ({"vat_rate": "0.2"}, "vat_rate_stated_by"),
    ({"vat_rate": "20%", "vat_rate_stated_by": "owner"}, "vat_rate"),                 # an exact decimal
    ({"vat_rate_stated_by": "site"}, "vat_rate_stated_by"),                           # nothing to attribute
])
def test_an_approved_sites_terms_are_whole_or_refused(change, message):
    site = {**_site(), **change}
    site = {key: value for key, value in site.items() if value is not None}
    with pytest.raises(ValidationError, match=message):
        Register.model_validate({"brand": "blokport", "sites": [site]})


def test_an_unapproved_site_declares_no_terms():
    site = {key: value for key, value in _site(decision="pending").items() if key != "price_basis"}
    assert Register.model_validate({"brand": "blokport", "sites": [site]}).sites[0].price_basis is None


@pytest.mark.parametrize("missing", ["price_basis", "decided_by", "decided_on"])
def test_every_sheet_competitor_declares_its_terms_and_decision(missing):
    with pytest.raises(ValidationError, match=missing):
        _layouts({key: value for key, value in _competitor().items() if key != missing})


# --- register.json: the register as the run started ------------------------------------------------------------------
def test_every_run_folder_carries_the_register_and_its_manifest_lists_it(tmp_path):
    register = _register(_site(vat_rate="0.21", vat_rate_stated_by="site"),
                         _site(site_id="later", base_url="https://later.example", decision="pending",
                               price_basis=None))
    result = collect_run(register, _shop(_page([_listing()])), MOMENT, _no_pause)
    run_dir = write_run(result, tmp_path)
    document = json.loads((run_dir / config.REGISTER_FILE).read_text(encoding="utf-8"))
    assert (document["brand"], document["contract_version"], document["run_id"]) == ("blokport", "4", result.run_id)
    keys = [row["source_key"] for row in document["sources"]]
    assert keys == sorted(keys) and "shop" in keys and "later" not in keys          # approved sites only
    assert all(set(row) == REGISTER_KEYS for row in document["sources"])
    shop = next(row for row in document["sources"] if row["source_key"] == "shop")
    assert shop == {
        "brand": "blokport", "source_key": "shop", "source": "site", "seller_name": "Shop", "seller_type": "merchant",
        "country_iso_2": "be", "served_market": "NL", "price_basis": "list", "net_discount": None,
        "net_discount_stated_by": None, "vat_rate": "0.21", "vat_rate_stated_by": "site",
        "seller_is_our_supplier": False, "supplier_key": None, "decided_by": "owner", "decided_on": "2026-11-08"}
    # the committed sheet competitors ride along whatever the run read (Cereser's Carrara list is net, the owner's)
    carrara = next(row for row in document["sources"] if row["source_key"] == "cereser_carrara")
    assert (carrara["source"], carrara["price_basis"], carrara["net_discount"], carrara["net_discount_stated_by"],
            carrara["country_iso_2"]) == ("file", "net", "0.2", "owner", "it")
    s3 = _S3()
    published = publish_run(s3, "bucket", "blokport", run_dir)
    assert config.REGISTER_FILE in published["manifest"]["files"]


def test_the_register_file_is_byte_identical_on_a_rewrite(tmp_path):
    result = collect_run(_register(), _shop(_page([_listing()])), MOMENT, _no_pause)
    first = (write_run(result, tmp_path) / config.REGISTER_FILE).read_bytes()
    assert (write_run(result, tmp_path) / config.REGISTER_FILE).read_bytes() == first


# --- the regular price (Mas's sale rule) -----------------------------------------------------------------------------
def test_a_store_api_listing_on_sale_gives_its_current_and_its_regular_price():
    on_sale = _listing(1, price_html="<del>&euro;&nbsp;25,00</del> <ins>&euro;&nbsp;21,25</ins>",
                       prices={"price": "2125", "regular_price": "2500", "sale_price": "2125",
                               "currency_code": "EUR", "currency_minor_unit": 2})
    (row,) = collect_run(_register(), _shop(_page([on_sale])), MOMENT, _no_pause).observations
    assert (row.price_text, row.price_amount) == ("€ 21,25", "21.25")
    assert (row.regular_price_text, row.regular_price_amount) == ("€ 25,00", "25.00")


def test_a_store_api_listing_at_its_regular_price_has_no_regular_price():
    (row,) = collect_run(_register(), _shop(_page([_listing(1)])), MOMENT, _no_pause).observations
    assert (row.price_amount, row.regular_price_text, row.regular_price_amount) == ("21.25", None, None)
