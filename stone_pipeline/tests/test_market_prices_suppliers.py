"""Supplier prices are not in market_prices (contract v4): they are a separate contract, delivered
under <env>/supplier_prices/<brand>/. market_prices knows the product scraper's suppliers only to refuse them:

- each of the 11 fetchers (the REGISTRY sources and the dormant ScraperBase modules) declares its `site_domain` once,
  derived from the module's own site URL where it has one;
- a register site whose host is a supplier's domain, and a sheet competitor whose site_domain is one, are refused at
  load, named; neither takes a `supplier_key`, and an observation carries no supplier column.
Fixtures only: nothing here reaches a site."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from pydantic import ValidationError

from market_prices.layouts import LayoutError, Layouts, load_layouts
from market_prices.register import Register, RegisterError, check_suppliers, load_register
from market_prices.schema import Observation, columns
from market_prices.text import same_site
from scrapers import run as scrapers_run

REPO = Path(__file__).resolve().parents[2]
DOMAINS = {"polonine": "polonine.slabware.com", "ferraz": "www.ferrazbrasilinventory.com", "develi": "marbleslabs.tr"}
EXPECTED_DOMAINS = {
    "marenostone": "marenostone.com", "polonine": "polonine.slabware.com", "zucchi": "inventory.zucchistones.com",
    "varsha": "varshastones.slabware.com", "fuleistone": "stone.fuleistone.com", "sentas": "sentasmarble.com",
    "develi": "marbleslabs.tr", "ferraz": "www.ferrazbrasilinventory.com", "temmer": "temmermarble.com",
    "tureks": "www.tureks.com", "brumagran": "www.brumagraninventory.com",
}


def _site(site_id="shop", base_url="https://shop.example", **overrides) -> dict:
    site = {
        "brand": "blokport", "site_id": site_id, "seller_name": "Shop", "seller_type": "merchant",
        "seller_country": "BE", "served_market": "NL", "platform": "woocommerce_store", "base_url": base_url,
        "terms": {"terms_url": f"{base_url}/terms", "checked_on": "2026-11-02", "summary": "No clause on copying."},
        "price_basis": "list", "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-08",
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


# --- the register: a supplier's site is refused, named ---------------------------------------------------------------
REFUSED = "supplier prices are not in market_prices"


def test_a_site_on_a_suppliers_domain_is_refused_by_name():
    named = rf"site shop: polonine.slabware.com is the domain of supplier polonine: {REFUSED}"
    with pytest.raises(RegisterError, match=named):
        check_suppliers(_register(_site(base_url="https://polonine.slabware.com")), DOMAINS)
    with pytest.raises(RegisterError, match=rf"supplier ferraz: {REFUSED}"):
        check_suppliers(_register(_site(base_url="https://ferrazbrasilinventory.com")), DOMAINS)   # www. on one side


def test_another_tenant_of_a_suppliers_platform_is_no_supplier():
    check_suppliers(_register(_site("other", base_url="https://other.slabware.com")), DOMAINS)


def test_a_site_takes_no_supplier_key():
    with pytest.raises(ValidationError, match="supplier_key"):
        _register(_site(base_url="https://polonine.slabware.com", supplier_key="polonine"))


def test_the_register_load_refuses_a_site_of_the_product_scrapers_suppliers(tmp_path):
    document = {"brand": "blokport", "sites": [_site(base_url="https://temmermarble.com")]}
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(RegisterError, match=rf"supplier temmer: {REFUSED}"):
        load_register("blokport", tmp_path)


@pytest.mark.parametrize("brand", ["blokport", "wudport"])
def test_the_committed_registers_hold_no_suppliers_site(brand):
    domains = scrapers_run.supplier_domains()
    sites = load_register(brand).sites
    assert not [site.site_id for site in sites if any(same_site(site.base_url, d) for d in domains.values())]


def test_an_observation_carries_no_supplier_column():
    assert not {"seller_is_our_supplier", "supplier_key"} & set(columns(Observation))


# --- a price sheet's competitor ---------------------------------------------------------------------------------------
def _layouts(site_domain="ferraz-rival.example", **extra) -> dict:
    competitor = {"competitor_id": "ferraz_br", "names": ["Ferraz"], "seller_type": "wholesaler",
                  "seller_country": "BR", "served_market": "GB", "site_domain": site_domain, "price_basis": "list",
                  "decided_by": "owner (supplied workbook) via Mas", "decided_on": "2026-11-13", **extra}
    return {"brand": "blokport", "competitors": [competitor], "layouts": [{
        "layout_id": "ferraz_csv", "header_row": 2, "headers": ["Code", "Product", "Price"],
        "competitor": {"cell": "A1"}, "listing_key": ["Code"], "listing_text": ["Product"], "price": "Price",
        "number": {"decimal": ".", "thousands": ","}}]}


def _load(tmp_path, document: dict) -> Layouts:
    (tmp_path / "blokport.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_layouts("blokport", tmp_path)


def test_a_sheet_competitor_declares_its_site_domain_as_a_bare_host():
    with pytest.raises(ValueError, match="site_domain"):
        Layouts.model_validate(_layouts(site_domain=None))
    for bad in ("https://ferraz.example", "ferraz.example/prices", "", "ferraz example"):
        with pytest.raises(ValueError):
            Layouts.model_validate(_layouts(site_domain=bad))


def test_a_sheet_competitor_on_a_suppliers_domain_is_refused_by_name(tmp_path):
    with pytest.raises(LayoutError, match=rf"competitor ferraz_br: www.ferrazbrasilinventory.com is the domain of "
                                          rf"supplier ferraz: {REFUSED}"):
        _load(tmp_path, _layouts(site_domain="www.ferrazbrasilinventory.com"))
    assert _load(tmp_path, _layouts(site_domain="other.slabware.com")).competitors[0].competitor_id == "ferraz_br"


def test_a_sheet_competitor_takes_no_supplier_key():
    with pytest.raises(ValidationError, match="supplier_key"):
        Layouts.model_validate(_layouts(supplier_key="ferraz"))


# --- the contract ----------------------------------------------------------------------------------------------------
def test_the_contract_says_supplier_prices_are_a_separate_contract():
    text = (REPO / "market_prices" / "CONTRACT.md").read_text(encoding="utf-8")
    assert ("**Supplier prices** are not in market_prices: they are delivered under "
            "`<env>/supplier_prices/<brand>/`") in text
    # the Versions paragraph keeps the history (version 2 added the columns); the field table names them no more
    fields = text.split("## observations.csv", 1)[1].split("\n## ", 1)[0]
    assert "seller_is_our_supplier" not in fields and "supplier_key" not in fields
