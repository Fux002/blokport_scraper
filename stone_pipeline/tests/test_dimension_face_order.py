"""The flat-dims convention, enforced: length is the LONGER plan face and width the shorter one; height stays the
thickness. derive orders the two faces once, after the unit correction and after an operator override (an override
is ordered too), so a vendor that lists the short side first (prod POL-536, 2.03 x 2.84) ships 2.84 x 2.03.
The provenance follows the values: each face keeps its own method label and flags, and the swap itself is recorded as
`faces:ordered`.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive
from stone_pipeline.state.overrides import load_overrides

_SIGNED = "fons,2026-11-10,office"


@pytest.fixture
def ref():
    return loaders.load_all()


def _slab(**kw):
    base = dict(src_site="polonine", surrogate_key="536", raw_format="Slab", variation_name="Verde Ubatuba",
                finish_name="Polished", type_name="Granite", color_name="Green", type_method="variety_authoritative",
                raw_thickness="2cm")
    base.update(kw)
    return CanonicalRow(**base)


def _derive(ref, row, overrides=None):
    ref.overrides = overrides
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    return row


def _methods(row) -> list[str]:
    return row.dimension_method.split(",")


def test_a_width_first_listing_ships_the_long_face_as_length(ref):
    # raw 'length' is one face, raw 'height' the other (the short face by the vendor's word, here the long one)
    row = _derive(ref, _slab(raw_dimensions="length=203cm;height=284cm"))
    assert (row.length, row.width, row.height) == (2.84, 2.03, 0.02)
    assert {"length:parsed", "width:parsed", "faces:ordered"} <= set(_methods(row))


def test_an_ordered_listing_is_unchanged_and_records_no_swap(ref):
    row = _derive(ref, _slab(raw_dimensions="length=284cm;height=203cm"))
    assert (row.length, row.width) == (2.84, 2.03)
    assert "faces:ordered" not in _methods(row)


def test_an_override_is_ordered_too(ref, tmp_path):
    path = tmp_path / "manual_overrides.csv"
    path.write_text("src_site,surrogate_key,field,value,confirmed_by,confirmed_at,source\n"
                    f"polonine,536,length,2.03,{_SIGNED}\npolonine,536,width,2.84,{_SIGNED}\n", encoding="utf-8")
    row = _derive(ref, _slab(), load_overrides(path))
    assert (row.length, row.width) == (2.84, 2.03)
    assert {"length:override", "width:override", "faces:ordered"} <= set(_methods(row))


def test_the_provenance_follows_the_swapped_values(ref):
    # length parsed 1.8 m, width missing -> the slab default 2.00 m, the longer: the default becomes the length,
    # and its dimension_defaulted flag and method label move with it
    row = _derive(ref, _slab(raw_dimensions="length=180cm"))
    assert (row.length, row.width) == (2.0, 1.8)
    assert {"length:defaulted", "width:parsed", "faces:ordered"} <= set(_methods(row))
    assert [f.field for f in row.review_flags if f.code == FlagCode.dimension_defaulted] == ["length"]


def test_a_face_whose_fetch_failed_is_never_ordered(ref):
    # a face whose fetch failed stays None (held for retry, dimension_unavailable): there is nothing to order
    row = _derive(ref, _slab(raw_dimensions="length=203cm", fetch_failed_fields=["width"]))
    assert row.width is None and row.length == 2.03
    assert "faces:ordered" not in _methods(row)


def test_the_unit_correction_comes_before_the_order(ref):
    # The case that tells the two orders apart. The unit-less '203' is centimetres read as metres. Corrected
    # first it is 2.03 m, the SHORTER face, so there is no swap: L 2.5, W 2.03. Ordered first it would sort as the
    # longer (203 > 2.5), be swapped into length and then corrected, leaving L 2.03 < W 2.5.
    row = _derive(ref, _slab(raw_dimensions="length=2.5;height=203"))
    assert (row.length, row.width) == (2.5, 2.03)
    assert "width:cm_to_m" in _methods(row) and "faces:ordered" not in _methods(row)
    assert [f.field for f in row.review_flags if f.code == FlagCode.dimension_unit_corrected] == ["width"]


def test_a_zero_face_is_never_ordered(ref):
    # a parsed 0 is a data error: it stays where the vendor put it, so validate rejects it (never swapped into width)
    row = _derive(ref, _slab(raw_dimensions="length=0m;height=1.97m"))
    assert (row.length, row.width) == (0.0, 1.97)
    assert "faces:ordered" not in _methods(row)
