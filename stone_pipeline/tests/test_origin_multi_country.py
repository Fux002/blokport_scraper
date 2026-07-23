"""Multi-country origin selection.

origin_map may list SEVERAL candidate countries for a (variety, type) when the same trade name is quarried
in more than one place ("Patagonia,Quartzite,BR,IN"). derive_origin picks the one that fits the supplier's
home country, with a per-supplier override lane and a review flag when no candidate matches. Covers the
rules agreed in the origin redesign:

  a  scraped origin present            -> use it (wins over everything)
  2  per-supplier override set         -> use it (a reseller decision, scoped to one source)
  c  single candidate                  -> use it (regardless of supplier home)
  b  multi + home is a candidate       -> the home country
  d  multi + home NOT a candidate      -> first candidate, LOW confidence + origin_multi_home_absent flag
  e  no map row                        -> supplier home, LOW confidence + origin_supplier_default flag
"""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import OriginMap, OriginOverrides, OriginRule, _norm
from stone_pipeline.stages import derive


@pytest.fixture(scope="module")
def base_ref():
    return loaders.load_all()


def _map(*rules):
    # rules: (variety, stone_type, country_iso) where country_iso may be "BR,IN"
    return OriginMap(rules=[OriginRule(variety=v, country_iso=c, city="", county="", stone_type=t)
                            for v, t, c in rules])


def _ref(base_ref, origin_map, overrides=None):
    return dataclasses.replace(base_ref, origin_map=origin_map,
                               origin_overrides=overrides or OriginOverrides())


def _cfg(home_iso):
    # start from a real source (valid loader shape), pin only the supplier home country
    return dataclasses.replace(load_source("varsha"), origin_default=home_iso)


def _row(**kw):
    base = dict(src_site="t", surrogate_key="1", raw_format="Slab", finish_name="Polished",
                type_name="Quartzite", color_name="White")
    base.update(kw)
    return CanonicalRow(**base)


def _origin_flags(row):
    return [f for f in row.review_flags if f.field == "origin"]


# --- rule c: single candidate ------------------------------------------------
def test_single_country_used_regardless_of_home(base_ref):
    ref = _ref(base_ref, _map(("Steel Grey", "Granite", "IN")))
    row = _row(src_site="zucchi", variation_name="Steel Grey", type_name="Granite")
    derive.derive_origin(row, ref, _cfg("BR"))          # home BR, but the stone is single-origin IN
    assert row.origin_country_code == "IN"
    assert row.origin_confidence == "high"
    assert not _origin_flags(row)


# --- rule b: multi, supplier home is one of the candidates -------------------
def test_multi_picks_supplier_home_br(base_ref):
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="zucchi", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("BR"))
    assert row.origin_country_code == "BR"
    assert row.origin_confidence == "high"
    assert not _origin_flags(row)


def test_multi_picks_supplier_home_in(base_ref):
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="varsha", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("IN"))
    assert row.origin_country_code == "IN"
    assert row.origin_confidence == "high"
    assert not _origin_flags(row)


# --- rule d: multi, supplier home NOT a candidate -> flag for review ---------
def test_multi_home_absent_flags_for_review(base_ref):
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="turco", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("TR"))          # Turkish trader; TR not in {BR,IN}
    assert row.origin_country_code == "BR"              # first candidate, provisional (a real origin)
    assert row.origin_confidence == "low"
    assert any(f.code == FlagCode.origin_multi_home_absent for f in _origin_flags(row))


# --- rung 2: per-supplier override wins over the map -------------------------
def test_supplier_override_wins(base_ref):
    ov = OriginOverrides(rules={(_norm("varsha"), _norm("Patagonia"), _norm("Quartzite")): "IN"})
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")), overrides=ov)
    row = _row(src_site="varsha", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("BR"))          # home BR would pick BR; the override pins IN
    assert row.origin_country_code == "IN"
    assert row.origin_source == "supplier_override"
    assert row.origin_confidence == "high"


def test_override_is_scoped_to_its_source(base_ref):
    # override for varsha must NOT affect zucchi's identical variety
    ov = OriginOverrides(rules={(_norm("varsha"), _norm("Patagonia"), _norm("Quartzite")): "IN"})
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")), overrides=ov)
    row = _row(src_site="zucchi", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("BR"))
    assert row.origin_country_code == "BR"              # zucchi still resolves by home (rule b)
    assert row.origin_source == "origin_map"


# --- rule a: scraped origin still wins over the map/override -----------------
def test_scraped_origin_wins(base_ref):
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="varsha", variation_name="Patagonia", raw_origin="Brazil")
    derive.derive_origin(row, ref, _cfg("IN"))
    assert row.origin_country_code == "BR"
    assert row.origin_source == "scrape_field"


# --- rule e: no map row -> supplier home fallback, flagged -------------------
def test_no_rule_falls_back_to_supplier_home(base_ref):
    ref = _ref(base_ref, _map())                        # empty map
    row = _row(src_site="varsha", variation_name="Unknown Stone", type_name="Marble")
    derive.derive_origin(row, ref, _cfg("IN"))
    assert row.origin_country_code == "IN"
    assert row.origin_source == "supplier_default"
    assert any(f.code == FlagCode.origin_supplier_default for f in _origin_flags(row))
