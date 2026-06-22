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
    assert row.title == "Walnut Travertine Honed Slab"


def test_title_strips_parenthetical_alias(ref, cfg):
    row = _slab_row(variation_name="Carrara (Bianco Carrara)", finish_name="Polished")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    assert row.title == "Carrara Polished Slab"


def test_dimensions_prefer_parsed(ref, cfg):
    row = _slab_row(raw_dimensions="length=2.80m;height=1.97m", raw_thickness="2cm")
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    assert row.length == 2.8 and row.height == 1.97
    assert row.width == 0.02  # thickness parsed to metres
    assert "length:parsed" in row.dimension_method


def test_description_template_reads_origin(ref, cfg):
    row = _slab_row(origin_city="Carrara", origin_country_code="IT")
    derive.derive_category(row, ref)
    derive.derive_description(row)
    assert "Carrara, IT" in row.description
    assert row.description_method == "template"


def test_handle_is_namespaced_and_stable(ref, cfg):
    row = _slab_row(variation_name="Walnut Travertine", finish_name="Honed", surrogate_key="620")
    derive.derive_category(row, ref)
    derive.derive_title(row)
    derive.derive_handle(row, cfg)
    assert row.handle == row.slug
    assert row.handle.endswith("-pol-620")
