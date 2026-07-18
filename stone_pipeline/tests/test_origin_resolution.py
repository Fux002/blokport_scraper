"""Origin-resolution audit tests.

Locks in the behaviour of the origin component after the bug review:
- _to_iso validates 2-letter tokens against the real ISO set and resolves aliases (UK->GB);
- OriginMap.lookup is exact-then-pattern, whole-word, and DETERMINISTIC (longest pattern wins);
- load_origin_map skips unusable rows (blank country / unknown match_type) instead of stamping a
  blank/garbage country at confidence, and is loud (not silent) when the file is missing;
- derive_origin's tier order, confidences, and flags are correct, incl. a present-but-unresolvable
  scraped origin correctly falling through.
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
    base = dict(src_site="t", surrogate_key="1", raw_format="Slab",
                finish_name="Polished", type_name="Granite", color_name="Green")
    base.update(kw)
    return CanonicalRow(**base)


def _cfg_with_default(cfg, default):
    return dataclasses.replace(cfg, origin_default=default)


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


# --- OriginMap.lookup --------------------------------------------------------
def _map(*rows):
    return OriginMap(rules=[OriginRule(*r) for r in rows])

def test_lookup_exact_variety():
    m = _map(("variety", "Agata Black", "BR", "", ""))
    assert m.lookup("agata black").country_iso == "BR"

def test_lookup_pattern_whole_word():
    m = _map(("pattern", "persa", "BR", "", ""))
    assert m.lookup("Brandnew Persa Gold").country_iso == "BR"

def test_lookup_pattern_not_substring():
    # 'oman' must NOT match inside 'Romano'; 'india' must NOT match inside 'Indiana'.
    m = _map(("pattern", "oman", "OM", "", ""), ("pattern", "india", "IN", "", ""))
    assert m.lookup("Romano Classico") is None
    assert m.lookup("Indiana Limestone") is None

def test_lookup_pattern_deterministic_longest_wins():
    # a name carrying TWO pattern tokens resolves to the longer (more specific) one, regardless of
    # the order the rules were loaded -- no CSV-row-order dependence.
    rows = [("pattern", "china", "CN", "", ""), ("pattern", "amarillo", "ES", "", "")]
    assert _map(*rows).lookup("China Amarillo").country_iso == "ES"
    assert _map(*reversed(rows)).lookup("China Amarillo").country_iso == "ES"

def test_lookup_exact_beats_pattern():
    m = _map(("pattern", "persa", "BR", "", ""), ("variety", "Persa Special", "IN", "", ""))
    assert m.lookup("Persa Special").country_iso == "IN"

def test_lookup_miss():
    assert _map(("variety", "Foo", "BR", "", "")).lookup("Totally Unknown") is None


# --- load_origin_map ---------------------------------------------------------
def _write(tmp_path, body):
    p = tmp_path / "origin_map.csv"
    p.write_text("match_type,pattern,country_iso,city,county\n" + body, encoding="utf-8")
    return p

def test_load_skips_blank_country(tmp_path):
    # a variety row with no country must NOT become a hit that stamps a blank country.
    m = load_origin_map(_write(tmp_path, "variety,Foo,BR,,\nvariety,Bar,,,\n"))
    assert m.lookup("Foo").country_iso == "BR"
    assert m.lookup("Bar") is None

def test_load_skips_unknown_match_type(tmp_path):
    # a typo'd match_type ('varietyy') is dropped, not silently treated as a pattern.
    m = load_origin_map(_write(tmp_path, "varietyy,Foo,BR,,\nvariety,Bar,IN,,\n"))
    assert m.lookup("Foo") is None
    assert m.lookup("Bar").country_iso == "IN"

def test_load_blank_match_type_not_defaulted_to_pattern(tmp_path):
    m = load_origin_map(_write(tmp_path, ",persa,BR,,\n"))
    assert m.lookup("Brandnew Persa Gold") is None  # the blank-kind row was skipped

def test_load_missing_file_is_loud_and_empty(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        m = load_origin_map(tmp_path / "does_not_exist.csv")
    assert m.rules == []
    assert any("origin_map missing" in r.getMessage() for r in caplog.records)

def test_load_real_map_has_both_rule_kinds():
    m = load_origin_map()  # the live catalog_source file
    kinds = {r.match_type for r in m.rules}
    assert "variety" in kinds and "pattern" in kinds
    assert m.lookup("Agata Black").country_iso == "BR"


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

def test_tier2_origin_map_exact_unconfirmed_is_flagged(ref, cfg):
    # an exact rule from the (unverified) origin_map.csv ships the country at medium, but FLAGGED --
    # the frozen-snapshot origins surface for review until an operator confirms each one.
    row = _row(variation_name="Agata Black")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_unconfirmed"
    assert row.origin_confidence == "medium"
    assert any(f.code == FlagCode.origin_unconfirmed for f in row.review_flags)


def test_tier2_confirmed_rule_ships_clean(ref, cfg):
    # a confirmed rule (operator-minted or verified) is the real origin: high confidence, no review flag
    r = dataclasses.replace(ref, origin_map=_map(("variety", "Agata Black", "BR", "", "", True)))
    row = _row(variation_name="Agata Black")
    derive.derive_origin(row, r, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_confirmed"
    assert row.origin_confidence == "high"
    assert not any(f.field == "origin" for f in row.review_flags)


def test_tier2b_pattern_no_longer_emits_falls_to_supplier(ref, cfg):
    # a name PATTERN ('persa') is NO LONGER an emitted origin: exact() excludes patterns, so a
    # pattern-only variety falls to the flagged supplier default (pattern survives only as a suggestion).
    row = _row(variation_name="Brandnew Persa Gold XL", raw_origin="")
    derive.derive_origin(row, ref, _cfg_with_default(cfg, "IN"))
    assert row.origin_source == "supplier_default"
    assert any(f.code == FlagCode.origin_supplier_default for f in row.review_flags)

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
def test_exact_type_scoped_beats_type_blind():
    # 'Aqua Blue' is Brazil in general, but the Onyx one is Iran. A type-blind row is the default; a
    # type-scoped row overrides it for that type only.
    m = OriginMap(rules=[
        OriginRule("variety", "Aqua Blue", "BR", "", ""),                       # type-blind (any type)
        OriginRule("variety", "Aqua Blue", "IR", "", "", stone_type="Onyx"),    # scoped to Onyx
    ])
    assert m.exact("aqua blue", "Onyx").country_iso == "IR"     # type-scoped wins
    assert m.exact("aqua blue", "Granite").country_iso == "BR"  # other type -> type-blind fallback
    assert m.exact("aqua blue").country_iso == "BR"             # no type given -> type-blind
    assert m.exact("aqua blue", None).country_iso == "BR"

def test_exact_type_scoped_only_does_not_leak_to_other_types():
    # a rule scoped to Onyx must NOT stamp a Granite (or type-unknown) row -- better unresolved than wrong.
    m = OriginMap(rules=[OriginRule("variety", "Aqua Blue", "IR", "", "", stone_type="Onyx")])
    assert m.exact("aqua blue", "Onyx").country_iso == "IR"
    assert m.exact("aqua blue", "Granite") is None
    assert m.exact("aqua blue") is None

def test_load_origin_map_parses_stone_type_column(tmp_path):
    p = tmp_path / "origin_map.csv"
    p.write_text("match_type,pattern,stone_type,country_iso,city,county\n"
                 "variety,Aqua Blue,,BR,,\n"           # type-blind
                 "variety,Aqua Blue,Onyx,IR,,\n",      # type-scoped
                 encoding="utf-8")
    m = load_origin_map(p)
    assert m.exact("Aqua Blue", "Onyx").country_iso == "IR"
    assert m.exact("Aqua Blue", "Marble").country_iso == "BR"   # falls back to type-blind

def test_load_origin_map_without_type_column_is_type_blind(tmp_path):
    # backward compatibility: a CSV with no stone_type column loads every row as type-blind (old behavior).
    p = tmp_path / "origin_map.csv"
    p.write_text("match_type,pattern,country_iso,city,county\nvariety,Foo,BR,,\n", encoding="utf-8")
    m = load_origin_map(p)
    assert m.exact("Foo").country_iso == "BR"
    assert m.exact("Foo", "Granite").country_iso == "BR"        # any type resolves to the type-blind row

def test_overlay_type_scoped_does_not_clobber_other_type():
    # a mint scoped to Onyx overrides only the Onyx origin; the type-blind and other-type rows survive.
    m = OriginMap(rules=[
        OriginRule("variety", "Aqua Blue", "BR", "", ""),
        OriginRule("variety", "Aqua Blue", "US", "", "", stone_type="Onyx"),
    ])
    added = m.apply_origin_overlay({("aqua blue", "onyx"): "IR"})
    assert added == 1
    onyx = m.exact("aqua blue", "Onyx")
    assert onyx.country_iso == "IR" and onyx.confirmed is True  # minted override, this type only
    assert m.exact("aqua blue", "Granite").country_iso == "BR"  # type-blind rule untouched

def test_derive_origin_uses_row_type_for_homonym(ref, cfg):
    # end-to-end: two products, same variety name, different type -> different emitted origin.
    import dataclasses
    r = dataclasses.replace(ref, origin_map=OriginMap(rules=[
        OriginRule("variety", "Aqua Blue", "BR", "", "", confirmed=True),
        OriginRule("variety", "Aqua Blue", "IR", "", "", confirmed=True, stone_type="Onyx"),
    ]))
    onyx = _row(variation_name="Aqua Blue", type_name="Onyx", raw_origin="")
    granite = _row(variation_name="Aqua Blue", type_name="Granite", raw_origin="")
    derive.derive_origin(onyx, r, cfg)
    derive.derive_origin(granite, r, cfg)
    assert onyx.origin_country_code == "IR"
    assert granite.origin_country_code == "BR"
