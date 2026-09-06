"""Per-vendor origin gate (opt-in `primary_origin`).

A vendor that declares its own primary quarry country does NOT inherit a per-variety map origin blindly:
the origin is trusted only when the map CORROBORATES the vendor -- the map's origin for this (variety, type)
is exactly that one country. Any other case (a different country, a multi-country map row, or no map row) is
held for a one-time origin confirmation (origin_needs_confirmation), never silently shipped. A vendor with no
primary_origin is byte-identical to the classic map + supplier-default ladder.

Self-contained (controlled origin map + a real source cfg), runs in CI.
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
def ref():
    return loaders.load_all()


def _row(**kw):
    base = dict(src_site="marenostone", surrogate_key="1", raw_format="Slab",
                finish_name="Polished", type_name="Granite", color_name="White",
                variation_name="Crystal White", type_method="variety_authoritative")
    base.update(kw)
    return CanonicalRow(**base)


def _cfg(primary_origin="IR", origin_default="IT"):
    return dataclasses.replace(load_source("marenostone"),
                               primary_origin=primary_origin, origin_default=origin_default)


def _with_map(ref, *rows):
    return dataclasses.replace(ref, origin_map=OriginMap(rules=[OriginRule(*r) for r in rows]))


def test_aligned_map_uses_vendor_origin(ref):
    # map says exactly IR for this (variety, type) and the vendor's primary origin is IR -> corroborated.
    row = _row()
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IR", "", "", "Granite")), _cfg())
    assert row.origin_country_code == "IR"
    assert row.origin_source == "vendor_origin"


def test_mismatch_holds_for_confirmation(ref):
    # map says India, vendor is IR -> NOT corroborated -> held (blank origin) + flagged for the origin queue.
    row = _row()
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IN", "", "", "Granite")), _cfg())
    assert not (row.origin_country_code or "")
    assert row.origin_source == "origin_needs_confirmation"
    assert any(f.code == FlagCode.origin_needs_confirmation for f in row.review_flags)


def test_multi_country_map_resolves_when_vendor_is_one_of_them(ref):
    # MEMBERSHIP: IR is one of the variety's documented origins ([IR, IN]) and IR is the vendor's primary,
    # so the vendor sources it from a real quarry country -> resolve IR (do not hold). This is what makes the
    # operator's "add a country to the variety" action actually resolve for that vendor.
    row = _row()
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IR,IN", "", "", "Granite")), _cfg())
    assert row.origin_country_code == "IR"
    assert row.origin_source == "vendor_origin"


def test_multi_country_map_holds_when_vendor_country_absent(ref):
    # The variety is quarried in [IN, TR] but NOT the vendor's IR -> genuinely unknown for this vendor -> hold.
    row = _row()
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IN,TR", "", "", "Granite")), _cfg())
    assert not (row.origin_country_code or "")
    assert row.origin_source == "origin_needs_confirmation"


def test_no_map_entry_holds(ref):
    row = _row()
    derive.derive_origin(row, _with_map(ref), _cfg())          # empty map
    assert row.origin_source == "origin_needs_confirmation"


def test_non_authoritative_row_is_not_gated_into_the_origin_queue(ref):
    # A row whose variety did NOT bind (type not variation-authoritative) must NOT be held for origin
    # confirmation -- it has no real variety to confirm an origin for, and holding it surfaced the RAW
    # scraped name ("Arabescato Marble Slab") in the origin queue. It falls to the classic supplier-default
    # path instead, so the origin queue only ever contains cleanly-bound varieties.
    row = _row(variation_name="", raw_name="Arabescato Marble Slab", type_method="type_name_fallback")
    derive.derive_origin(row, _with_map(ref, ("Arabescato", "BR", "", "", "Marble")), _cfg())
    assert row.origin_source != "origin_needs_confirmation"     # NOT in the origin queue
    assert row.origin_source == "supplier_default"


def test_no_primary_origin_uses_classic_ladder_unchanged(ref):
    # opt-out: a vendor without primary_origin behaves exactly as before (the map value is used).
    row = _row()
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IN", "", "", "Granite")),
                         _cfg(primary_origin=""))
    assert row.origin_country_code == "IN"
    assert row.origin_source == "origin_map"


def test_scraped_origin_wins_over_gate(ref):
    row = _row(raw_origin="Brazil")
    derive.derive_origin(row, _with_map(ref, ("Crystal White", "IN", "", "", "Granite")), _cfg())
    assert row.origin_country_code == "BR"
    assert row.origin_source == "scrape_field"


def test_stored_confirmation_override_wins_over_gate(ref):
    # a confirmed answer is stored as a per-vendor override -> it resolves at step 2, never re-asked.
    ov = OriginOverrides(rules={(_norm("marenostone"), _norm("Crystal White"), _norm("Granite")): "IR"})
    r = dataclasses.replace(ref, origin_map=OriginMap(rules=[OriginRule("Crystal White", "IN", "", "", "Granite")]),
                            origin_overrides=ov)
    row = _row()
    derive.derive_origin(row, r, _cfg())
    assert row.origin_country_code == "IR"
    assert row.origin_source == "supplier_override"
