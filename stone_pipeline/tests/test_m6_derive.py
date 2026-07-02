"""M6 DoD: a row with slab_count derives bundle size at high confidence; a row
without it defaults with a flag; a generated title and description match the real
upload style.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def cfg():
    return load_source("polonine")


def _slab_row(**kw):
    base = dict(src_site="polonine", surrogate_key="620", raw_format="Slab",
                variation_name="Verde Ubatuba", finish_name="Polished", type_name="Granite",
                color_name="Green")
    base.update(kw)
    return CanonicalRow(**base)


def test_bundle_from_slab_count_high(ref, cfg):
    row = _slab_row(raw_slab_count="10")
    derive.derive_category(row, ref)
    derive.derive_bundle_size(row, ref, cfg)
    assert row.bundle_size == 10
    assert row.bundle_size_method == "explicit_slab_count"
    assert row.bundle_size_confidence == "high"


def test_bundle_defaults_with_flag(ref, cfg):
    row = _slab_row()  # no count, no area
    derive.derive_category(row, ref)
    derive.derive_bundle_size(row, ref, cfg)
    assert row.bundle_size == cfg.default_bundle_size
    assert any(f.code == FlagCode.bundle_default for f in row.review_flags)


def test_parsed_weight_is_tonnes_not_kilograms(ref, cfg):
    # units.csv converts weight to kg; the emitted Product Weight (and synthetic fill) is tonnes.
    # A scraped '300 kg' slab must ship 0.3 t, not 300 -- so parsed and synthetic agree on unit.
    row = _slab_row(raw_weight="300 kg")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert abs(row.weight - 0.3) < 1e-6
    row2 = _slab_row(raw_weight="0.3 ton")
    derive.derive_category(row2, ref)
    derive.derive_dimensions(row2, ref)
    assert abs(row2.weight - 0.3) < 1e-6  # 0.3 t == 300 kg -> same tonnes value


def test_bundle_zero_count_falls_through_to_default(ref, cfg):
    # A sold-out slab reports slab_count '0'; '0'.isdigit() is True but a bundle of
    # zero is contradictory -- it must fall through, never emit sold_in_bundle + size 0.
    row = _slab_row(raw_slab_count="0")
    derive.derive_category(row, ref)
    derive.derive_bundle_size(row, ref, cfg)
    assert row.sold_in_bundle is True
    assert row.bundle_size == cfg.default_bundle_size
    assert row.bundle_size != 0


def test_bundle_from_area_division(ref, cfg):
    row = _slab_row(raw_total_m2="55.0", raw_per_slab_m2="5.5")
    derive.derive_category(row, ref)
    derive.derive_bundle_size(row, ref, cfg)
    assert row.bundle_size == 10
    assert row.bundle_size_method == "area_division"


def test_block_has_no_bundle(ref, cfg):
    row = _slab_row(raw_format="Block")
    derive.derive_category(row, ref)
    derive.derive_bundle_size(row, ref, cfg)
    assert row.sold_in_bundle is False
    assert row.bundle_size is None


def test_title_matches_upload_style(ref, cfg):
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Walnut Travertine Honed"   # variety + finish, NO category word


def test_title_strips_parenthetical_alias(ref, cfg):
    row = _slab_row(variation_name="Carrara (Bianco Carrara)", finish_name="Polished")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Carrara Polished"


def test_dimensions_prefer_parsed(ref, cfg):
    row = _slab_row(raw_dimensions="length=2.80m;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 2.8 and row.height == 1.97
    assert row.width == 0.02  # thickness parsed to metres
    assert "length:parsed" in row.dimension_method


def test_dimension_range_uses_midpoint_not_low_metres(ref, cfg):
    # regression: '2-3 cm' must parse to the 2.5 cm midpoint (0.025 m), NOT the low endpoint '2'
    # read as 2 metres (the old first-number-only parse made a 2 cm slab 2 m thick).
    row = _slab_row(raw_dimensions="length=2.80m;height=1.97m", raw_thickness="2-3 cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.width == 0.025, f"range thickness should be the 2.5 cm midpoint, got {row.width}"


def test_bundle_count_from_slabs_array_is_robust():
    # regression: the per-slab key count must survive spacing/case/quote variants and prefer a real
    # JSON parse, not a literal .count('"n"') that a spacing/case variant silently read as 0.
    from stone_pipeline.stages.derive import _count_slab_entries
    assert _count_slab_entries('[{"n":1},{"n":2},{"n":3}]') == 3
    assert _count_slab_entries('{"x":[{ "N" : 1},{ "n":2}]}') == 2
    assert _count_slab_entries('[{"Numero": 5},{"Numero": 6}]') == 2
    assert _count_slab_entries("not json at all") == 0


def test_description_uses_resolved_format_not_slab_default(ref, cfg):
    # regression: a block must be described as a 'block', never defaulted to 'slab'; and an
    # unresolved colour must not be invented as 'a natural <type>'.
    row = _slab_row(raw_format="Block")
    derive.derive_category(row, ref)
    derive.derive_description(row)
    d = row.description.lower()
    assert "block" in d and " slab" not in d, f"block mislabelled: {row.description}"
    assert "is a natural " not in d, f"invented a colour: {row.description}"


def test_tile_dimensions_are_tile_sized(ref, cfg):
    # a tile with no scraped dimensions must get TILE-sized synthetic dims (~0.3-0.6m face,
    # ~1-2cm thick), NOT slab-sized (1.5-3m) -- sources often ship tiles with no dimensions.
    row = _slab_row(raw_format="Tile")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert 0.3 <= row.length <= 0.6 and 0.3 <= row.height <= 0.6   # tile face, not slab
    assert 0.01 <= row.width <= 0.02                               # tile thickness ~1-2cm
    assert row.length < 1.0 and row.height < 1.0                   # definitively not slab-sized


def test_description_template_reads_origin(ref, cfg):
    row = _slab_row(origin_city="Carrara", origin_country_code="IT")
    derive.derive_category(row, ref)
    derive.derive_description(row)
    assert "Carrara, IT" in row.description
    assert row.description_method == "template"


def test_ports_belong_to_supplier(ref, cfg):
    # ports come from the source (supplier) BY NAME, resolved via ports.csv to real
    # port ids, independent of the stone's origin
    row = _slab_row(origin_country_code="IN")
    derive.derive_ports(row, ref, cfg)
    assert row.port_ids == [ref.ports.resolve(p) for p in cfg.ports_default]
    assert row.port_ids and all(pid in ref.ports.iso_by_port for pid in row.port_ids)


# a synthetic name no exact origin_map rule and no geographic pattern can resolve, so the
# fallback tiers below are genuinely exercised even as the real map grows.
_UNKNOWN_VARIETY = "Zzx Nowhere Fictional Stone"


def test_origin_falls_back_to_supplier_default_flagged(ref, cfg):
    # no scraped country + variety not in origin_map -> stamp the supplier's default country as a
    # LOW-confidence fallback (Medusa requires an origin for pricing rules) and flag it for review
    # so origin_map can be expanded with the real per-variety origin.
    row = _slab_row(variation_name=_UNKNOWN_VARIETY, raw_origin="")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_country_code == cfg.origin_default      # the supplier default ("IT")
    assert row.origin_source == "supplier_default"
    assert row.origin_confidence == "low"
    assert any(f.code == FlagCode.origin_supplier_default for f in row.review_flags)


def test_origin_unresolved_when_no_supplier_default(ref):
    # a source with no origin_default and no scrape/map origin stays UNRESOLVED + flagged; the
    # Process gate rejects it rather than emit a Medusa-breaking blank.
    cfg = load_source("polonine")
    cfg.origin_default = ""
    row = _slab_row(variation_name=_UNKNOWN_VARIETY, raw_origin="")
    derive.derive_origin(row, ref, cfg)
    assert not row.origin_country_code
    assert row.origin_source == "unresolved"
    assert any(f.code == FlagCode.origin_unresolved for f in row.review_flags)


def test_origin_pattern_generalizes_to_new_variety(ref, cfg):
    # a brand-new variety the exact tier has never seen still resolves via a geographic name PATTERN
    # ('persa' -> BR), carrying origin onto new variants -- emitted at medium confidence but FLAGGED
    # so it surfaces for a one-time confirm into the master reference.
    row = _slab_row(variation_name="Verde Persa Imperiale XL", raw_origin="")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_pattern"
    assert any(f.code == FlagCode.origin_pattern_guess for f in row.review_flags)


def test_origin_accepts_country_name(ref, cfg):
    row = _slab_row(raw_origin="India")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_country_code == "IN"      # name resolved via country_codes
    assert row.origin_source == "scrape_field"


def test_handle_is_namespaced_and_stable(ref, cfg):
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed", surrogate_key="620")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    derive.derive_handle(row, cfg)
    assert row.handle == row.slug
    assert row.handle.endswith("-pol-620")
