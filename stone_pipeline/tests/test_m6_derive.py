"""M6 DoD: a row with slab_count derives bundle size at high confidence; a row
without it defaults with a flag; a generated title and description match the real
upload style.
"""

from __future__ import annotations

import dataclasses

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
    # a sellable, searchable title: variety + finish + material. The format word (Slab/Tile/Block) is
    # NOT in the title -- format is a variant dimension, not product identity.
    row = _slab_row(variation_name="Carrara", finish_name="Honed", type_name="Marble")
    derive.derive_category(row, ref)   # format resolves to Slab ...
    derive.derive_title(row)
    assert row.title == "Carrara Honed Marble"   # ... but the title never carries it
    assert "Slab" not in row.title


def test_title_skips_material_already_in_variety_name(ref, cfg):
    # 'Travertine' is already in the variety name -> never repeat it ('...Travertine Travertine')
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed", type_name="Travertine")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Walnut Travertine Honed"


def test_title_multiword_material_not_doubled(ref, cfg):
    # a multi-word type whose head noun is already in the name is skipped whole -> no 'Sandstone Sandstone'
    row = _slab_row(variation_name="Rain Forest Sandstone", finish_name="Honed",
                    type_name="Quartzitic Sandstone")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Rain Forest Sandstone Honed"
    assert row.title.lower().count("sandstone") == 1


def test_block_title_drops_finish_and_raw(ref, cfg):
    # a block is uncut stone -> no finish word in the title (no 'Blue Pearl Raw'); material stays, and the
    # format word (Block) is not in the title
    row = _slab_row(raw_format="Block", variation_name="Blue Pearl", finish_name="Raw",
                    type_name="Granite")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Blue Pearl Granite"
    assert "Raw" not in row.title and "Block" not in row.title


def test_title_omits_format_word(ref, cfg):
    # the format word is never in the title, whether the format resolves or not (it lives only in the
    # description prose). Here it is unresolved; test_sellable_title covers the resolved case.
    row = _slab_row(variation_name="Carrara", finish_name="Honed", type_name="Marble", raw_format="")
    derive.derive_title(row)
    assert row.title == "Carrara Honed Marble"


def test_title_strips_parenthetical_alias(ref, cfg):
    row = _slab_row(variation_name="Carrara (Bianco Carrara)", finish_name="Polished", type_name="Marble")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Carrara Polished Marble"
    assert "Bianco" not in row.title


def test_dimensions_prefer_parsed(ref, cfg):
    row = _slab_row(raw_dimensions="length=2.80m;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 2.8 and row.height == 1.97
    assert row.width == 0.02  # thickness parsed to metres
    assert "length:parsed" in row.dimension_method


def test_thickness_range_defaults_to_standard(ref, cfg):
    # policy: a thickness given as a range ('2-3 cm') is ambiguous -> the standard 2 cm (the European stocked
    # depth), flagged. NOT the 2.5 cm midpoint and NEVER the low '2' read as 2 metres (the old parse bug).
    row = _slab_row(raw_dimensions="length=2.80m;height=1.97m", raw_thickness="2-3 cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.width == 0.02, f"range thickness should default to the 2 cm standard, got {row.width}"
    assert any(f.code == FlagCode.dimension_defaulted and f.field == "width" for f in row.review_flags)


def test_face_range_uses_maximum(ref, cfg):
    # a face dimension given as a range ('105 to 145cm') resolves to its MAX (1.45 m) -- cut smaller later.
    row = _slab_row(raw_dimensions="length=105 to 145cm;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 1.45 and "length:parsed" in row.dimension_method


def test_multi_thickness_defaults_to_standard_keeps_faces(ref, cfg):
    # a bundle with several thicknesses ('MULTI') is ambiguous -> standard 2 cm, flagged; real faces kept.
    row = _slab_row(raw_dimensions="length=3.20m;height=1.90m", raw_thickness="MULTI")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 3.2 and row.height == 1.9 and row.width == 0.02
    assert any(f.code == FlagCode.dimension_defaulted and f.field == "width" for f in row.review_flags)


def test_free_length_fills_only_missing_and_keeps_real_dims(ref, cfg):
    # a cut-to-size tile ('length=Free') keeps its real height + thickness; only the missing length is
    # filled from the tile standard (0.6 m), flagged.
    row = _slab_row(raw_format="Tile", raw_dimensions="length=Free;height=40cm", raw_thickness="1.8cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.height == 0.4 and row.width == 0.018      # real values preserved
    assert row.length == 0.6                             # tile standard fills the 'Free' length only
    assert any(f.code == FlagCode.dimension_defaulted and f.field == "length" for f in row.review_flags)


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


def test_missing_dimensions_filled_from_pack_default(ref, cfg):
    # a row with NO scraped dims is filled entirely from the category standard (slab 3.3 x 2.0 x 0.02 m),
    # every filled dim flagged, so validate no longer rejects it as dimension_invalid.
    from stone_pipeline.stages import validate
    row = _slab_row()   # no raw_dimensions / raw_thickness on the row
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert (row.length, row.height, row.width) == (3.3, 2.0, 0.02)
    assert {f.field for f in row.review_flags if f.code == FlagCode.dimension_defaulted} \
        == {"length", "height", "width"}
    validate.validate_row(row)
    assert not any(r.rule == "dimension_invalid" for r in row.reject_reasons)   # filled, not rejected


def test_parsed_zero_dimension_is_kept_and_rejected(ref, cfg):
    # a real 0 is a data error, NOT a missing value: it is kept (never defaulted) so validate rejects it.
    from stone_pipeline.stages import validate
    row = _slab_row(raw_dimensions="length=0m;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 0.0
    assert not any(f.code == FlagCode.dimension_defaulted and f.field == "length" for f in row.review_flags)
    validate.validate_row(row)
    assert any(r.rule == "dimension_invalid" for r in row.reject_reasons)


def test_fetch_failed_dimensions_held_not_defaulted(ref, cfg):
    # a dimension whose source FETCH failed (recoverable) is HELD -- left None + flagged
    # dimension_unavailable, NEVER defaulted -- so freight is never computed from a fabricated size.
    # Contrast with a genuine absence (test_missing_dimensions_filled_from_pack_default), which defaults.
    from stone_pipeline.stages import validate
    row = _slab_row(fetch_failed_fields=["dims"])   # no raw dims + a recorded fetch failure
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length is None and row.width is None and row.height is None
    codes = {f.code for f in row.review_flags}
    assert FlagCode.dimension_unavailable in codes and FlagCode.dimension_defaulted not in codes
    assert "length:unavailable" in row.dimension_method
    validate.validate_row(row)
    assert any(r.rule == "dimension_unavailable" for r in row.reject_reasons)   # held for retry
    assert not any(r.rule == "dimension_invalid" for r in row.reject_reasons)   # not "bad data"
    assert not row.is_emittable


def test_fetch_failed_thickness_only_holds_width_keeps_real_faces(ref, cfg):
    # partial failure: only the thickness fetch failed -> width HELD, the real faces kept (not held).
    row = _slab_row(raw_dimensions="length=3.2;height=2.0", fetch_failed_fields=["thickness"])
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 3.2 and row.height == 2.0     # real faces preserved
    assert row.width is None                            # thickness held, not defaulted
    assert any(f.code == FlagCode.dimension_unavailable and f.field == "width" for f in row.review_flags)
    assert not any(f.code == FlagCode.dimension_unavailable and f.field in ("length", "height")
                   for f in row.review_flags)


def test_fetch_failed_dim_retries_and_emits_when_present_next_scrape(ref, cfg):
    # the retry is stateless (like no_image): the SAME product with dims present next scrape derives a real
    # size and no longer holds. (No fetch_failed marker -> normal parse.)
    from stone_pipeline.stages import validate
    row = _slab_row(raw_dimensions="length=2.8m;height=1.97m", raw_thickness="2cm")   # fetch succeeded
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert (row.length, row.height, row.width) == (2.8, 1.97, 0.02)
    validate.validate_row(row)
    assert not any(r.rule in ("dimension_unavailable", "dimension_invalid") for r in row.reject_reasons)


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
    assert row.title == "Carrara Honed Marble"               # display carries material, NOT format
    assert row.handle == "carrara-honed-pol-620"             # URL keys off variety+finish only
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


def test_pattern_only_variety_no_longer_auto_emits_origin(ref, cfg):
    # a name PATTERN ('persa' -> BR) is NO LONGER an emitted origin: a look-alike named after a stone is
    # not quarried in that stone's country. A variety the exact map has never seen falls to the flagged
    # supplier default (the review worklist); the pattern survives only as the :4200 mint suggestion.
    row = _slab_row(variation_name="Verde Persa Imperiale XL", raw_origin="")
    derive.derive_origin(row, ref, cfg)
    assert row.origin_source == "supplier_default"
    assert any(f.code == FlagCode.origin_supplier_default for f in row.review_flags)


def test_origin_map_exact_ignores_name_patterns():
    from stone_pipeline.reference.loaders import OriginMap, OriginRule
    m = OriginMap(rules=[OriginRule("variety", "Blue Pearl", "NO", "", ""),
                         OriginRule("pattern", "persa", "BR", "", "")])
    assert m.exact("Blue Pearl").country_iso == "NO"        # exact variety rule
    assert m.exact("Verde Persa") is None                   # a pattern is NOT an exact origin
    assert m.lookup("Verde Persa").country_iso == "BR"      # but IS a suggestion (for :4200)


def test_minted_origin_overlays_map_as_confirmed():
    # the effective per-variety map = CSV + minted seed_country, overlaid as CONFIRMED, operator wins
    from stone_pipeline.reference.loaders import OriginMap, OriginRule, _norm
    m = OriginMap(rules=[OriginRule("variety", "Blue Pearl", "NO", "", "", confirmed=False)])
    added = m.apply_origin_overlay({(_norm("Blue Pearl"), ""): "IN", (_norm("Brand New Stone"), ""): "CN"})
    assert added == 2
    bp = m.exact("Blue Pearl")
    assert bp.country_iso == "IN" and bp.confirmed is True   # minted overrides the CSV country
    assert m.exact("Brand New Stone").country_iso == "CN"    # a new variety is added, confirmed


def test_derive_origin_unconfirmed_map_hit_is_flagged(ref, cfg):
    # a real exact rule from the (unverified) origin_map.csv ships the country but FLAGS it for review
    from stone_pipeline.reference.loaders import OriginMap, OriginRule
    r = dataclasses.replace(ref, origin_map=OriginMap(
        rules=[OriginRule("variety", "Testonia", "BR", "", "", confirmed=False)]))
    row = _slab_row(variation_name="Testonia", raw_origin="")
    derive.derive_origin(row, r, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_unconfirmed"
    assert any(f.code == FlagCode.origin_unconfirmed for f in row.review_flags)


def test_derive_origin_confirmed_map_hit_ships_clean(ref, cfg):
    # a confirmed rule (operator-minted or verified) is the real origin: no review flag
    from stone_pipeline.reference.loaders import OriginMap, OriginRule
    r = dataclasses.replace(ref, origin_map=OriginMap(
        rules=[OriginRule("variety", "Testonia", "BR", "", "", confirmed=True)]))
    row = _slab_row(variation_name="Testonia", raw_origin="")
    derive.derive_origin(row, r, cfg)
    assert row.origin_country_code == "BR"
    assert row.origin_source == "origin_confirmed"
    assert not any(f.field == "origin" for f in row.review_flags)


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
