"""Every name the scraper COMMITS is a fixed point of its field's rule (plan 4.1): the committed data is cleaned once
(the cleanup script) and stays clean, because every writer cleans what it emits.

Only scraper-owned files are checked. from_medusa/<env>/attributes.csv and ports.csv are the backend's copies,
re-fetched, never edited here; the backend cleans them and its VALIDATE step proves them (PHASE-0 runbook 4c).
`merged_from` is excluded: it is the audit record of what a merge removed, kept as it was."""

from __future__ import annotations

import csv
import json
from functools import partial
from pathlib import Path

import pytest

from stone_pipeline.core.text import clean_alias_list
from stone_pipeline.reference.loaders import field_kinds as fk

ROOT = Path(__file__).resolve().parents[2]
_alias = partial(fk.display, fk.VARIATION_ALIAS_VALUE)


def _csv(path):
    with open(ROOT / path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _not_fixed(field, values, rule=fk.display):
    return sorted({v for v in values if v and rule(field, v) != v})


@pytest.mark.parametrize("path", ["from_medusa/development/variants_export_base.csv",
                                  "from_medusa/production/variants_export_base.csv"])
def test_base_names_and_aliases_are_fixed_points(path):
    rows = _csv(path)
    assert _not_fixed(fk.VARIATION_NAME, (r["Name"] for r in rows)) == []
    bad = [r["Key"] for r in rows
           if clean_alias_list(r["Name"], [a for a in r["Aliases"].split("|") if a], _alias)
           != ([a for a in r["Aliases"].split("|") if a], [])]
    assert bad == []


def _posts():
    files = sorted((ROOT / "catalog_source").glob("backbone_*.json")) + sorted(
        (ROOT / "catalog_source" / "backbone_additions").glob("*.json"))
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for post in doc["posts"] if isinstance(doc, dict) else doc:
            yield path.name, post


def test_backbone_names_values_and_aliases_are_fixed_points():
    bad = []
    for name, p in _posts():
        if fk.clean(fk.VARIATION_NAME, p["variant"]) != p["variant"]:
            bad.append((name, "variant", p["variant"]))
        for field, values in ((fk.TYPE_NAME, [p.get("stone_type") or ""]), (fk.COLOR_NAME, p.get("color") or []),
                              (fk.FINISH_NAME, p.get("finishes") or []), (fk.QUALITY_NAME, p.get("qualities") or [])):
            bad += [(name, field, v) for v in _not_fixed(field, values, fk.clean)]   # Medusa spellings, case kept
        aliases = list(p.get("aliases") or [])
        if clean_alias_list(p["variant"], aliases, _alias) != (aliases, []):
            bad.append((name, "aliases", p["variant"]))
    assert bad[:20] == [] and len(bad) == 0


def test_scraper_owned_tables_are_fixed_points():
    origin = _csv("catalog_source/origin_map.csv")
    assert _not_fixed(fk.VARIATION_NAME, (r["variety"] for r in origin), fk.clean) == []
    assert _not_fixed(fk.PRODUCT_ORIGIN_CITY, (r["city"] for r in origin), fk.clean) == []
    assert _not_fixed(fk.PRODUCT_ORIGIN_COUNTY, (r["county"] for r in origin), fk.clean) == []
    assert _not_fixed(fk.VARIATION_NAME, (r["Name"] for r in _csv("catalog_source/missing_variants.csv"))) == []
    ports = _csv("catalog_source/ports.csv")
    assert _not_fixed(fk.PORT_NAME, (r["name"] for r in ports), fk.clean) == []
    assert _not_fixed(fk.PORT_REGION_NAME, (r["region_name"] for r in ports), fk.clean) == []
