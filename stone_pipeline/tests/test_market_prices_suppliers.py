"""A competitor that is also one of the owner's suppliers is flagged in the market data (owner's ruling via Mas,
11-11, part of contract v2). The flag is derived from the product scraper's own suppliers, never hand-typed:

- each of the 11 fetchers (the REGISTRY sources and the dormant ScraperBase modules) declares its `site_domain` once,
  derived from the module's own site URL where it has one;
- a register site names its supplier by the fetcher's source id (`supplier_key`); an unknown key, a supplier's domain
  without the key, or a key whose domain is not the site's all refuse at register load;
- the row copies `seller_is_our_supplier` and `supplier_key` from the register.
Fixtures only: nothing here reaches a site."""

from __future__ import annotations

import ast
import csv
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from market_prices import intake
from market_prices.collect import _observation, stamp_for
from market_prices.layouts import LayoutError, Layouts, load_layouts
from market_prices.register import Register, RegisterError, check_suppliers, load_register
from market_prices.schema import ListingFacts, Observation, columns, csv_values
from market_prices.text import same_site
from scrapers import run as scrapers_run

REPO = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 11, 11, 9, 15, 0, tzinfo=timezone.utc)
DOMAINS = {"polonine": "polonine.slabware.com", "ferraz": "www.ferrazbrasilinventory.com", "develi": "marbleslabs.tr"}
EXPECTED_DOMAINS = {
    "marenostone": "marenostone.com", "polonine": "polonine.slabware.com", "zucchi": "inventory.zucchistones.com",
    "varsha": "varshastones.slabware.com", "fuleistone": "stone.fuleistone.com", "sentas": "sentasmarble.com",
    "develi": "marbleslabs.tr", "ferraz": "www.ferrazbrasilinventory.com", "temmer": "temmermarble.com",
    "tureks": "www.tureks.com", "brumagran": "www.brumagraninventory.com",
}


def _site(site_id="shop", base_url="https://shop.example", **overrides) -> dict:
    site = {
        "site_id": site_id, "seller_name": "Shop", "seller_type": "merchant", "seller_country": "BE",
        "served_market": "NL", "platform": "woocommerce_store", "base_url": base_url,
        "terms": {"terms_url": f"{base_url}/terms", "checked_on": "2026-11-02", "summary": "No clause on copying."},
        "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-08",
    }
    return {**site, **overrides}


def _register(*sites: dict) -> Register:
    return Register.model_validate({"brand": "blokport", "sites": list(sites)})


# --- one site_domain per fetcher ------------------------------------------------------------------------------------
def test_every_supplier_fetcher_declares_its_site_domain_and_no_two_share_one():
    domains = scrapers_run.supplier_domains()
    assert set(domains) == set(scrapers_run._SOURCES) | set(scrapers_run.DORMANT)
    assert domains == EXPECTED_DOMAINS
    assert len(set(domains.values())) == len(domains)


def _scraper_modules() -> dict[str, ast.ClassDef]:
    """scrapers/<name>.py -> the ScraperBase subclass it defines, read from the source (no import). A module named
    _<name> (the template) is not a supplier."""
    found = {}
    for path in sorted(p for p in (REPO / "scrapers").glob("*.py") if not p.stem.startswith("_")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and any(getattr(b, "id", "") == "ScraperBase" for b in node.bases):
                found[path.stem] = node
    return found


def test_the_dormant_list_is_every_scraperbase_module_outside_the_registry():
    assert set(_scraper_modules()) - set(scrapers_run._SOURCES) == set(scrapers_run.DORMANT)


def test_a_site_domain_is_derived_from_the_modules_own_url_except_develis():
    for name, node in _scraper_modules().items():
        (value,) = [stmt.value for stmt in node.body if isinstance(stmt, ast.Assign)
                    and any(getattr(target, "id", "") == "site_domain" for target in stmt.targets)]
        assert isinstance(value, ast.Constant) == (name == "develi"), name


def test_supplier_domains_refuse_an_empty_or_shared_domain(monkeypatch):
    real = scrapers_run._discover
    monkeypatch.setattr(scrapers_run, "_discover",
                        lambda name: type("X", (real(name),), {"site_domain": ""}) if name == "temmer" else real(name))
    with pytest.raises(RuntimeError, match="temmer"):
        scrapers_run.supplier_domains()
    monkeypatch.setattr(scrapers_run, "_discover", lambda name: type("X", (real(name),), {
        "site_domain": "temmermarble.com" if name == "tureks" else real(name).site_domain}))
    with pytest.raises(RuntimeError, match="temmermarble.com"):
        scrapers_run.supplier_domains()


# --- the match rule --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url, domain, same", [
    ("https://www.marbleslabs.tr", "marbleslabs.tr", True),          # www. on the site side only
    ("https://marbleslabs.tr", "www.marbleslabs.tr", True),          # www. on the supplier side only
    ("https://MarbleSlabs.TR/slabs/", "marbleslabs.tr", True),
    ("https://shop.polonine.slabware.com", "polonine.slabware.com", True),
    ("https://other.slabware.com", "polonine.slabware.com", False),  # another SlabWare tenant
    ("https://varshastones.slabware.com", "polonine.slabware.com", False),
    ("https://slabware.com", "polonine.slabware.com", False),
    ("https://notmarenostone.com", "marenostone.com", False),
    ("https://marenostone.com.example", "marenostone.com", False),
])
def test_a_site_is_a_suppliers_when_its_host_is_the_domain_or_under_it(url, domain, same):
    assert same_site(url, domain) is same


# --- the register checks, both ways ----------------------------------------------------------------------------------
def test_an_unknown_supplier_key_refuses():
    with pytest.raises(RegisterError, match="supplier_key 'nonexistent'"):
        check_suppliers(_register(_site(supplier_key="nonexistent")), DOMAINS)


def test_a_suppliers_domain_without_the_key_refuses():
    with pytest.raises(RegisterError, match="polonine"):
        check_suppliers(_register(_site(base_url="https://polonine.slabware.com")), DOMAINS)
    with pytest.raises(RegisterError, match="ferraz"):
        check_suppliers(_register(_site(base_url="https://ferrazbrasilinventory.com")), DOMAINS)


def test_a_supplier_key_on_another_domain_refuses():
    with pytest.raises(RegisterError, match="other.slabware.com"):
        check_suppliers(_register(_site(base_url="https://other.slabware.com", supplier_key="polonine")), DOMAINS)


def test_matching_sites_pass_and_other_slabware_tenants_need_no_key():
    check_suppliers(_register(_site("pol", base_url="https://polonine.slabware.com", supplier_key="polonine"),
                              _site("dev", base_url="https://www.marbleslabs.tr", supplier_key="develi"),
                              _site("other", base_url="https://other.slabware.com")), DOMAINS)


def test_the_register_load_runs_the_check_on_the_product_scrapers_suppliers(tmp_path):
    document = {"brand": "blokport", "sites": [_site(base_url="https://temmermarble.com")]}
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(RegisterError, match="temmer"):
        load_register("blokport", tmp_path)


@pytest.mark.parametrize("brand", ["blokport", "wudport"])
def test_the_committed_registers_hold_no_unflagged_supplier(brand):
    assert load_register(brand).brand == brand


# --- the row ---------------------------------------------------------------------------------------------------------
def _row(site: dict) -> Observation:
    facts = ListingFacts(listing_key="1", source_url=f"{site['base_url']}/p/1", listing_text="Slab")
    return _observation(facts, _register(site).sites[0], "blokport", stamp_for(MOMENT), "woo/1")


def test_the_row_copies_the_supplier_flag_from_the_register():
    flagged = _row(_site(base_url="https://polonine.slabware.com", supplier_key="polonine"))
    assert (flagged.seller_is_our_supplier, flagged.supplier_key) == (True, "polonine")
    plain = _row(_site())
    assert (plain.seller_is_our_supplier, plain.supplier_key) == (False, None)


def test_the_flag_is_two_columns_after_the_seller_and_written_true_or_false():
    names = columns(Observation)
    at = names.index("served_market")
    assert names[at + 1:at + 3] == ("seller_is_our_supplier", "supplier_key")
    cells = dict(zip(names, csv_values(_row(_site(base_url="https://polonine.slabware.com", supplier_key="polonine")))))
    assert (cells["seller_is_our_supplier"], cells["supplier_key"]) == ("true", "polonine")
    cells = dict(zip(names, csv_values(_row(_site()))))
    assert (cells["seller_is_our_supplier"], cells["supplier_key"]) == ("false", "")


def test_a_row_cannot_say_supplier_without_its_key_or_the_reverse():
    row = _row(_site()).model_dump()
    for changed in ({"seller_is_our_supplier": True}, {"supplier_key": "polonine"}):
        with pytest.raises(ValueError, match="supplier_key"):
            Observation.model_validate({**row, **changed})


# --- a price sheet's competitor ---------------------------------------------------------------------------------------
def _layouts(supplier_key=None) -> dict:
    competitor = {"competitor_id": "ferraz_br", "names": ["Ferraz"], "seller_type": "wholesaler",
                  "seller_country": "BR", "served_market": "GB"}
    if supplier_key:
        competitor["supplier_key"] = supplier_key
    return {"brand": "blokport", "competitors": [competitor], "layouts": [{
        "layout_id": "ferraz_csv", "header_row": 2, "headers": ["Code", "Product", "Price"],
        "competitor": {"cell": "A1"}, "listing_key": ["Code"], "listing_text": ["Product"], "price": "Price",
        "number": {"decimal": ".", "thousands": ","}}]}


def test_a_sheet_competitor_names_its_supplier_by_the_same_keys_and_an_unknown_one_refuses(tmp_path):
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(_layouts("ferraz")), encoding="utf-8")
    assert load_layouts("blokport", tmp_path).competitors[0].supplier_key == "ferraz"
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(_layouts("nonexistent")), encoding="utf-8")
    with pytest.raises(LayoutError, match="nonexistent"):
        load_layouts("blokport", tmp_path)


def test_a_sheet_row_copies_its_competitors_supplier_flag():
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows([["Ferraz"], ["Code", "Product", "Price"], ["F1", "Slab", "9"]])
    file = intake.IntakeFile(key="dev/market_prices/blokport/intake/ferraz.csv", version_id="v1",
                             last_modified=MOMENT, data=out.getvalue().encode())
    for key, flag in (("ferraz", True), (None, False)):
        (row,) = intake.absorb_file(file, Layouts.model_validate(_layouts(key)), "bucket", "blokport").observations
        assert (row.seller_is_our_supplier, row.supplier_key) == (flag, key)


# --- the contract ----------------------------------------------------------------------------------------------------
def test_the_contract_says_a_suppliers_list_price_is_its_own_line_never_the_buy_price():
    text = (REPO / "market_prices" / "CONTRACT.md").read_text(encoding="utf-8")
    assert "| seller_is_our_supplier, supplier_key |" in text
    assert "never as the buy price" in text
