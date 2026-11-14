"""register.json (contract v4, CONTRACT.md "register.json"): the brand's register as a run started, written into every
run folder so the consumer upserts its sources from the scraper's own register. One row per approved site and per
declared sheet competitor, sorted by source_key, with exactly the contract's keys; an empty value is null. The same
register always gives the same bytes."""

from __future__ import annotations

import json
from typing import Iterable

from market_prices import config
from market_prices.layouts import Competitor, Layouts
from market_prices.register import Decision, PriceTerms, Register, Site
from market_prices.schema import Source


def _terms(terms: PriceTerms) -> dict:
    return {"price_basis": terms.price_basis, "net_markup": terms.net_markup,
            "net_markup_stated_by": terms.net_markup_stated_by, "vat_rate": terms.vat_rate,
            "vat_rate_stated_by": terms.vat_rate_stated_by}


def _site_row(brand: str, site: Site) -> dict:
    return {"brand": brand, "source_key": site.site_id, "source": Source.site.value, "seller_name": site.seller_name,
            "seller_type": site.seller_type.value, "country_iso_2": site.seller_country.lower(),
            "served_market": site.served_market, **_terms(site),
            "decided_by": site.decided_by, "decided_on": site.decided_on.isoformat()}


def _competitor_row(brand: str, competitor: Competitor) -> dict:
    return {"brand": brand, "source_key": competitor.competitor_id, "source": Source.file.value,
            "seller_name": competitor.names[0], "seller_type": competitor.seller_type.value,
            "country_iso_2": competitor.seller_country.lower(), "served_market": competitor.served_market,
            **_terms(competitor), "decided_by": competitor.decided_by,
            "decided_on": competitor.decided_on.isoformat()}


def register_sources(register: Register, layouts: Layouts) -> tuple[dict, ...]:
    """Every approved site and every sheet competitor of the brand, as register.json rows, sorted by source_key."""
    if layouts.brand != register.brand:
        raise ValueError(f"the {layouts.brand} sheet layouts are not the {register.brand} register's")
    rows = [_site_row(register.brand, site) for site in register.sites if site.decision is Decision.approved]
    rows += [_competitor_row(register.brand, competitor) for competitor in layouts.competitors]
    return tuple(sorted(rows, key=lambda row: row["source_key"]))


def register_document(brand: str, run_id: str, sources: Iterable[dict]) -> bytes:
    document = {"brand": brand, "contract_version": config.CONTRACT_VERSION, "run_id": run_id,
                "sources": list(sources)}
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
