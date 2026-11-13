"""Multi-country origin selection.

origin_map may list SEVERAL candidate countries for a (variety, type) when the same trade name is quarried
in more than one place ("Patagonia,Quartzite,BR,IN"). derive_origin picks the one that fits the supplier's
home country, with a per-supplier override lane, and holds the row for origin confirmation when no candidate
matches. Covers the rules agreed in the origin redesign (rule d: owner ruling 2026-11-06, never guess):

  a  scraped origin present            -> use it (wins over everything)
  2  per-supplier override set         -> use it (a reseller decision, scoped to one source)
  c  single candidate                  -> use it (regardless of supplier home)
  b  multi + home is a candidate       -> the home country
  d  multi + home NOT a candidate      -> no origin, HELD for confirmation (origin_needs_confirmation queue)
  e  no map row                        -> supplier home, LOW confidence + origin_supplier_default flag
"""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import OriginMap, OriginOverrides, OriginRule, _norm
from stone_pipeline.stages import decisions, derive


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
                type_name="Quartzite", color_name="White", type_method="variety_authoritative")
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


# --- rule d: multi, supplier home NOT a candidate -> held for confirmation ---
def test_multi_home_absent_is_held_for_confirmation(base_ref, seeded_config_store):
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="turco", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("TR"))          # Turkish trader; TR not in {BR,IN}
    assert not (row.origin_country_code or "")         # never the first candidate: which one is unknown
    assert row.origin_source == "origin_needs_confirmation"
    flag = next(f for f in _origin_flags(row) if f.code == FlagCode.origin_needs_confirmation)
    assert flag.raw_value == "BR,IN" and flag.best_guess == "TR"
    # the SAME per-vendor origin queue a vendor-map mismatch goes to
    decisions.write_origin_confirm_file([row])
    item = ds.list_pending("origin")[0]
    assert (item["source"], item["variety"], item["map_country"], item["vendor_origin"]) == \
        ("turco", "Patagonia", "BR,IN", "TR")


def test_the_multi_home_card_names_the_supplier_home_and_prefills_no_origin(base_ref, seeded_config_store):
    # the supplier's HOME is not a declared own country and is by definition not a documented origin, so the
    # card says where the supplier is based and leaves the origin for the operator to choose
    from stone_pipeline.config.control import server
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN,TR")))
    row = _row(src_site="turco", variation_name="Patagonia")
    derive.derive_origin(row, ref, _cfg("IT"))
    decisions.write_origin_confirm_file([row])
    card = server._origin_as_card(ds.list_pending("origin")[0])
    assert "supplier based in IT; stone documented in BR, IN, TR" in card["reason"]
    assert "declares" not in card["reason"]
    assert card["origin"] == ""


def test_the_vendor_mismatch_card_keeps_the_declared_country_prefill(base_ref, seeded_config_store):
    from stone_pipeline.config.control import server
    ref = _ref(base_ref, _map(("Patagonia", "Quartzite", "BR,IN")))
    row = _row(src_site="turco", variation_name="Patagonia")
    derive.derive_origin(row, ref, dataclasses.replace(_cfg("IT"), primary_origin="TR"))
    decisions.write_origin_confirm_file([row])
    card = server._origin_as_card(ds.list_pending("origin")[0])
    assert "the vendor declares TR" in card["reason"]
    assert card["origin"] == "TR"


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
