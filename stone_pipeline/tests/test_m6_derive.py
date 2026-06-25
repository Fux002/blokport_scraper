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


def test_origin_unresolved_when_unknown_never_supplier(ref):
    # no scraped country + variety not in origin_map -> UNRESOLVED (flagged for review), NEVER the
    # supplier's country. The supplier is where the stone is sold from, not where it was quarried.
    row = _slab_row(variation_name="Verde Ubatuba", raw_origin="")
    derive.derive_origin(row, ref)
    assert not row.origin_country_code              # blank, not the supplier's "IT"
    assert row.origin_source == "unresolved"


def test_origin_accepts_country_name(ref):
    row = _slab_row(raw_origin="India")
    derive.derive_origin(row, ref)
    assert row.origin_country_code == "IN"      # name resolved via country_codes
    assert row.origin_source == "scrape_field"


def test_handle_is_namespaced_and_stable(ref, cfg):
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed", surrogate_key="620")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    derive.derive_handle(row, cfg)
    assert row.handle == row.slug
    assert row.handle.endswith("-pol-620")
