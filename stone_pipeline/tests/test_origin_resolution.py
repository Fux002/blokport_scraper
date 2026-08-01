"""Origin-resolution audit tests.

Locks in the behaviour of the origin component after the bug review:
- _to_iso validates 2-letter tokens against the real ISO set and resolves aliases (UK->GB);
- OriginMap.exact is a STRICT (name, type) match -- no fallback, no name-pattern suggestions;
- load_origin_map requires (variety + country_iso + stone_type) and skips a row missing any, loud when the
  file is missing;
- derive_origin's tier order, confidences, and flags are correct.
"""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import OriginMap, OriginRule, load_origin_map
from stone_pipeline.stages import derive


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def cfg():
    return load_source("varsha")  # has a real origin_default (IN)


def _row(**kw):
    # A normally-reconciled row: its type is variation-authoritative (from the variation Key), which is
    # what the curated (name, type) origin lookups require. Override type_method to test the fallback path.
    base = dict(src_site="t", surrogate_key="1", raw_format="Slab",
                finish_name="Polished", type_name="Granite", color_name="Green",
                type_method="variety_authoritative")
    base.update(kw)
    return CanonicalRow(**base)


def _cfg_with_default(cfg, default):
    return dataclasses.replace(cfg, origin_default=default)


def _map(*rows):
    # each row = (variety, country_iso, city, county[, stone_type])
    return OriginMap(rules=[OriginRule(*r) for r in rows])


# --- _to_iso -----------------------------------------------------------------
def test_to_iso_valid_code_passes(ref):
    assert derive._to_iso("IN", ref) == "IN"

def test_to_iso_country_name(ref):
    assert derive._to_iso("India", ref) == "IN"
    assert derive._to_iso("  italy ", ref) == "IT"   # casing + whitespace

def test_to_iso_uk_alias_resolves_to_gb_not_literal(ref):
    # the bug: 'UK' short-circuited to the literal (invalid) 'UK'; it must resolve to GB.
    assert derive._to_iso("UK", ref) == "GB"

def test_to_iso_bogus_two_letter_rejected(ref):
    # the bug: any 2-letter alpha passed through at high confidence. 'XX'/'EN' are not ISO codes.
    assert derive._to_iso("XX", ref) is None
    assert derive._to_iso("EN", ref) is None

def test_to_iso_empty(ref):
    assert derive._to_iso("", ref) is None
    assert derive._to_iso(None, ref) is None


# --- load_origin_map ---------------------------------------------------------
def _write(tmp_path, body):
    p = tmp_path / "origin_map.csv"
    p.write_text("variety,stone_type,country_iso,city,county\n" + body, encoding="utf-8")
    return p

def test_load_skips_blank_country(tmp_path):
    # a row with no country is a TODO to fill, not a rule that stamps a blank country.
    m = load_origin_map(_write(tmp_path, "Foo,Granite,BR,,\nBar,Granite,,,\n"))
    assert m.exact("Foo", "Granite").country_iso == "BR"
    assert m.exact("Bar", "Granite") is None

def test_load_skips_a_type_less_row(tmp_path):
    # stone_type is MANDATORY (origin is keyed by (name, TYPE)); a type-less row is skipped, not loaded.
    m = load_origin_map(_write(tmp_path, "Foo,,BR,,\n"))
    assert m.rules == []
    assert m.exact("Foo") is None and m.exact("Foo", "Granite") is None

def test_load_missing_file_is_loud_and_empty(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        m = load_origin_map(tmp_path / "does_not_exist.csv")
    assert m.rules == []
    assert any("origin_map missing" in r.getMessage() for r in caplog.records)

def test_load_real_map_is_all_typed_varieties():
    m = load_origin_map()  # the live catalog_source file
    assert m.rules and all(r.stone_type for r in m.rules)   # every loaded rule is type-scoped


# --- derive_origin tiers -----------------------------------------------------
def test_tier1_scrape_field_high(ref, cfg):
    row = _row(variation_name="Anything", raw_origin="Brazil")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "scrape_field"
    assert row.origin_confidence == "high"

def test_scrape_unresolvable_falls_through(ref, cfg):
    # raw_origin='XX' is not a real code -> must NOT be stamped; falls to the supplier default.
    row = _row(variation_name="Zzqq Unknown Variety Abc", raw_origin="XX")
    derive.derive_origin(row, ref, _cfg_with_default(cfg, "IN"))
    assert row.origin_country_code == "IN"
    assert row.origin_source == "supplier_default"

def test_tier2_origin_map_hit_ships_clean(ref, cfg):
    # the map is the source of truth: an exact (name, TYPE) hit is the real origin -- high conf, no flag.
    r = dataclasses.replace(ref, origin_map=_map(("Agata Black", "BR", "", "", "Granite")))
    row = _row(variation_name="Agata Black")   # type_name="Granite" (default) matches the typed rule
    derive.derive_origin(row, r, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_map"
    assert row.origin_confidence == "high"
    assert not any(f.field == "origin" for f in row.review_flags)


def test_tier3_supplier_default_low_and_flagged(ref, cfg):
    row = _row(variation_name="Zzqq Unknown Variety Abc")
    derive.derive_origin(row, ref, _cfg_with_default(cfg, "IN"))
    assert row.origin_country_code == "IN"
    assert row.origin_source == "supplier_default"
    assert row.origin_confidence == "low"
    assert any(f.code == FlagCode.origin_supplier_default for f in row.review_flags)

def test_tier4_unresolved_no_country_flagged(ref, cfg):
    row = _row(variation_name="Zzqq Unknown Variety Abc")
    derive.derive_origin(row, ref, _cfg_with_default(cfg, ""))
    assert not (row.origin_country_code or "")
    assert row.origin_source == "unresolved"
    assert any(f.code == FlagCode.origin_unresolved for f in row.review_flags)


# --- type-scoped origin: homonyms (same name, different stone type) -----------
def test_exact_is_strict_on_name_and_type():
    # origin is keyed by (name, TYPE). Each type has its own row; a type with NO row resolves to None
    # (unresolved -> review), NEVER a country guessed from a different type of the same name.
    m = OriginMap(rules=[
        OriginRule("Aqua Blue", "IR", "", "", stone_type="Onyx"),
        OriginRule("Aqua Blue", "BR", "", "", stone_type="Granite"),
    ])
    assert m.exact("aqua blue", "Onyx").country_iso == "IR"
    assert m.exact("aqua blue", "Granite").country_iso == "BR"
    assert m.exact("aqua blue", "Marble") is None              # no Marble row -> unresolved, not guessed
    assert m.exact("aqua blue") is None                        # no type given, no type-less row

def test_exact_type_scoped_only_does_not_leak_to_other_types():
    # a rule scoped to Onyx must NOT stamp a Granite (or type-unknown) row -- better unresolved than wrong.
    m = OriginMap(rules=[OriginRule("Aqua Blue", "IR", "", "", stone_type="Onyx")])
    assert m.exact("aqua blue", "Onyx").country_iso == "IR"
    assert m.exact("aqua blue", "Granite") is None
    assert m.exact("aqua blue") is None

def test_load_origin_map_parses_stone_type_column(tmp_path):
    p = tmp_path / "origin_map.csv"
    p.write_text("variety,stone_type,country_iso,city,county\n"
                 "Aqua Blue,Granite,BR,,\n"
                 "Aqua Blue,Onyx,IR,,\n",
                 encoding="utf-8")
    m = load_origin_map(p)
    assert m.exact("Aqua Blue", "Onyx").country_iso == "IR"
    assert m.exact("Aqua Blue", "Granite").country_iso == "BR"
    assert m.exact("Aqua Blue", "Marble") is None              # no Marble row -> strict miss, not a guess

def test_overlay_type_scoped_does_not_clobber_other_type():
    # a mint scoped to Onyx overrides ONLY the Onyx origin; another type's rule survives untouched.
    m = OriginMap(rules=[
        OriginRule("Aqua Blue", "BR", "", "", stone_type="Granite"),
        OriginRule("Aqua Blue", "US", "", "", stone_type="Onyx"),
    ])
    added = m.apply_origin_overlay({("aqua blue", "onyx"): "IR"})
    assert added == 1
    assert m.exact("aqua blue", "Onyx").country_iso == "IR"     # minted override, this type only
    assert m.exact("aqua blue", "Granite").country_iso == "BR"  # the Granite rule is untouched

def test_derive_origin_uses_row_type_for_homonym(ref, cfg):
    # end-to-end: two products, same variety name, different type -> different emitted origin.
    import dataclasses
    r = dataclasses.replace(ref, origin_map=OriginMap(rules=[
        OriginRule("Aqua Blue", "BR", "", "", stone_type="Granite"),
        OriginRule("Aqua Blue", "IR", "", "", stone_type="Onyx"),
    ]))
    onyx = _row(variation_name="Aqua Blue", type_name="Onyx", raw_origin="")
    granite = _row(variation_name="Aqua Blue", type_name="Granite", raw_origin="")
    derive.derive_origin(onyx, r, cfg)
    derive.derive_origin(granite, r, cfg)
    assert onyx.origin_country_code == "IR"
    assert granite.origin_country_code == "BR"


def test_derive_origin_refuses_curated_lookup_when_type_not_authoritative(ref, cfg):
    # F2 regression: a homonym whose type is NOT variation-authoritative (name-derived fallback, e.g. the
    # variation bound but its Key carried no known type slug) must NOT resolve a curated origin -- that
    # shipped a confidently-WRONG homonym origin. It drops to the supplier default and is flagged for type
    # verification, instead of silently picking one homonym's country.
    r = dataclasses.replace(ref, origin_map=OriginMap(rules=[
        OriginRule("Azul White", "BR", "", "", stone_type="Quartzite"),
        OriginRule("Azul White", "IR", "", "", stone_type="Onyx"),
    ]))
    row = _row(variation_name="Azul White", type_name="Quartzite", raw_origin="",
               type_method="type_name_fallback")               # NOT variation-authoritative
    derive.derive_origin(row, r, _cfg_with_default(cfg, "IN"))
    assert row.origin_source == "supplier_default"              # not "origin_map"
    assert row.origin_country_code == "IN"                      # the supplier default, not BR/IR
    assert any(f.code == FlagCode.origin_type_unverified for f in row.review_flags)
    # sanity: the SAME row WITH authoritative type does resolve the curated origin
    ok = _row(variation_name="Azul White", type_name="Quartzite", raw_origin="")  # variety_authoritative
    derive.derive_origin(ok, r, cfg)
    assert ok.origin_country_code == "BR" and ok.origin_source == "origin_map"
