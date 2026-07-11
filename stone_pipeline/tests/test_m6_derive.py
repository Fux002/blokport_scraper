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


def test_title_is_listing_style_with_material_and_format(ref, cfg):
    # a sellable, searchable title: variety + finish + material + format
    row = _slab_row(variation_name="Carrara", finish_name="Honed", type_name="Marble")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Carrara Honed Marble Slab"


def test_title_skips_material_already_in_variety_name(ref, cfg):
    # 'Travertine' is already in the variety name -> never repeat it ('...Travertine Travertine')
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed", type_name="Travertine")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Walnut Travertine Honed Slab"


def test_title_multiword_material_not_doubled(ref, cfg):
    # a multi-word type whose head noun is already in the name is skipped whole -> no 'Sandstone Sandstone'
    row = _slab_row(variation_name="Rain Forest Sandstone", finish_name="Honed",
                    type_name="Quartzitic Sandstone")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Rain Forest Sandstone Honed Slab"
    assert row.title.lower().count("sandstone") == 1


def test_block_title_drops_finish_and_raw(ref, cfg):
    # a block is uncut stone -> no finish word in the title (no 'Blue Pearl Raw'); material + Block stay
    row = _slab_row(raw_format="Block", variation_name="Blue Pearl", finish_name="Raw",
                    type_name="Granite")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Blue Pearl Granite Block"
    assert "Raw" not in row.title


def test_title_omits_format_word_when_unresolved(ref, cfg):
    # an unresolved format must not put a neutral 'Piece' in the title (unlike the description prose)
    row = _slab_row(variation_name="Carrara", finish_name="Honed", type_name="Marble", raw_format="")
    derive.derive_title(row)   # no derive_category -> format stays unresolved
    assert row.title == "Carrara Honed Marble"


def test_title_strips_parenthetical_alias(ref, cfg):
    row = _slab_row(variation_name="Carrara (Bianco Carrara)", finish_name="Polished", type_name="Marble")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Carrara Polished Marble Slab"
    assert "Bianco" not in row.title


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


def test_missing_dimensions_are_rejected_not_fabricated(ref, cfg):
    # dimensions are REQUIRED and never synthesised: a product with no scraped dims keeps None,
    # so validate rejects it rather than invent a size.
    from stone_pipeline.stages import validate
    row = _slab_row()   # no raw_dimensions / raw_thickness on the row
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length is None and row.width is None and row.height is None and row.weight is None
    validate.validate_row(row)
    assert not row.is_emittable
    assert any(r.rule == "dimension_invalid" for r in row.reject_reasons)   # rejected, not fabricated


def test_weight_derived_from_dimensions_and_density(ref, cfg):
    # no scraped weight -> weight = volume(real dims) x per-type density, in tonnes, flagged.
    # 3.2 x 0.03 x 2.0 x 2700(Granite) / 1000 = 0.5184 t
    row = _slab_row(raw_dimensions="length=3.2;height=2.0", raw_thickness="0.03m", type_name="Granite")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert abs(row.weight - 0.518) < 0.002
    assert "weight:derived" in row.dimension_method
    assert any(f.code == FlagCode.weight_derived for f in row.review_flags)


def test_description_template_reads_origin(ref, cfg):
    row = _slab_row(origin_city="Carrara", origin_country_code="IT")
    derive.derive_category(row, ref)
    derive.derive_description(row)
    assert "Carrara, IT" in row.description
    assert row.description_method == "template"


def test_description_includes_real_thickness_for_slab(ref, cfg):
    # width is the parsed thickness (2cm -> 20mm) -> the description carries a real buying spec
    row = _slab_row(raw_dimensions="length=2.8m;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    derive.derive_description(row)
    assert "at 20 mm" in row.description
    assert "it offers" in row.description   # warmer connective


def test_description_omits_thickness_for_block(ref, cfg):
    # a block never carries a slab thickness spec (its 'width' is a chunk dimension, not a thickness)
    row = _slab_row(raw_format="Block", raw_dimensions="length=1.5m;height=1.5m", raw_thickness="1.5m")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    derive.derive_description(row)
    assert "mm" not in row.description


def test_description_uses_benefit_led_finish_phrase(ref, cfg):
    # the sensory 'sell' lives in the finish phrase, tied to the real finish (honest, not adjective spam)
    row = _slab_row(finish_name="Polished")
    derive.derive_category(row, ref)
    derive.derive_description(row)
    assert "mirror-like" in row.description


def test_handle_is_decoupled_from_the_enriched_title(ref, cfg):
    # the URL keys off variety+finish, NOT the display title -> enriching the title never churns it
    row = _slab_row(variation_name="Carrara", finish_name="Honed", type_name="Marble")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    derive.derive_handle(row, cfg)
    assert row.title == "Carrara Honed Marble Slab"          # display carries material + format
    assert row.handle == "carrara-honed-pol-620"             # URL does not
    assert "marble" not in row.handle and "slab" not in row.handle
    assert row.handle == row.slug


def test_block_handle_keeps_finish_even_though_title_drops_it(ref, cfg):
    # the block title drops 'Raw', but the handle keeps it -> byte-identical to the pre-change URL
    row = _slab_row(raw_format="Block", variation_name="Blue Pearl", finish_name="Raw",
                    type_name="Granite", surrogate_key="42")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    derive.derive_handle(row, cfg)
    assert "Raw" not in row.title
    assert row.handle == "blue-pearl-raw-pol-42"


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
