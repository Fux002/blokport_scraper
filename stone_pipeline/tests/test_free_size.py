"""Free size: a listing whose vendor sells a side 'Free' (cut to order) is offered in the pack's shapes.

Owner, 2026-11-04: a square, a rectangle and a long thin one (long:short 1, 2, 4); a side the vendor states
is the short side; every side free -> the long side is 60 cm; a shape longer than 120 cm is not offered.
Each shape is its own listing: own key, own size, own pictures, made-to-order stock.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import domain
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive, free_size, validate
from stone_pipeline.stages.keys_dedupe import KeyCollisionError
from stone_pipeline.tests.test_domains import _valid_pack_dict, _write_pack


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def cfg():
    return load_source("marenostone")


def _tile(key="MAR-P/12/03", dims="length=Free;height=40cm", fmt="tile", **kw):
    base = dict(src_site="marenostone", surrogate_key=key, src_natural_key=key, raw_format=fmt,
                format_value=fmt, raw_dimensions=dims, raw_thickness="1.8cm",
                variation_name="Pietra Grey", finish_name="Honed", type_name="Marble", color_name="Grey")
    base.update(kw)
    return CanonicalRow(**base)


def _keys(rows):
    return [r.surrogate_key for r in rows]


# --- the pack -------------------------------------------------------------------------------------------

def test_the_stone_pack_offers_free_tiles_as_square_rectangle_and_long_thin_capped_at_120_cm():
    p = domain.load_pack("stone")
    assert p.free_size_words == ("free",)
    assert p.free_size_shapes == {"tile": domain.FreeSizeShapes(ratios=(1.0, 2.0, 4.0), long_side=0.6,
                                                                 max_length=1.2)}


@pytest.mark.parametrize("shapes, match", [
    ({"nope": {"ratios": [1], "long_side": 0.6, "max_length": 1.2}}, "not in the declared categories"),
    ({"x": {"ratios": [1], "long_side": 0.6}}, "exactly ratios, long_side, max_length"),
    ({"x": {"ratios": [], "long_side": 0.6, "max_length": 1.2}}, "ratios >= 1"),
    ({"x": {"ratios": [0.5], "long_side": 0.6, "max_length": 1.2}}, "ratios >= 1"),
    ({"x": {"ratios": [2, 2], "long_side": 0.6, "max_length": 1.2}}, "distinct"),
    ({"x": {"ratios": [1], "long_side": 0, "max_length": 1.2}}, "long_side must be a positive"),
    ({"x": {"ratios": [1], "long_side": 0.6, "max_length": True}}, "max_length must be a positive"),
])
def test_a_malformed_free_size_shapes_entry_fails_loud_at_load(tmp_path, monkeypatch, shapes, match):
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["free_size_words"] = ["free"]
    pack["free_size_shapes"] = shapes
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match=match):
        domain.load_pack("bad")


# --- the shapes (the owner's table) ---------------------------------------------------------------------

@pytest.mark.parametrize("dims, keys", [
    ("length=Free;height=40cm", ["K-40x40", "K-80x40"]),
    ("length=Free;height=60cm", ["K-60x60", "K-120x60"]),
    ("length=Free;height=70cm", ["K-70x70"]),
    ("length=Free;height=80cm", ["K-80x80"]),
    ("length=Free;height=Free", ["K-60x60", "K-60x30", "K-60x15"]),
    ("length=40cm;height=Free", ["K-40x40", "K-80x40"]),
], ids=["40 wide", "60 wide", "70 wide", "80 wide", "every side free", "stated length, free width"])
def test_each_free_size_listing_becomes_its_shapes_under_the_cap(ref, dims, keys):
    rows, fanned = free_size.run([_tile(key="K", dims=dims)], ref)
    assert fanned == 1
    assert _keys(rows) == keys


def test_the_shapes_replace_the_listing_in_place_and_leave_the_vendor_record_untouched(ref):
    sized = dict(dims="length=60cm;height=40cm")
    rows, _ = free_size.run([_tile(key="A", **sized), _tile(), _tile(key="Z", **sized)], ref)
    assert _keys(rows) == ["A", "MAR-P/12/03-40x40", "MAR-P/12/03-80x40", "Z"]
    assert {r.src_natural_key for r in rows[1:3]} == {"MAR-P/12/03"}
    assert {r.raw_dimensions for r in rows[1:3]} == {"length=Free;height=40cm"}
    # each shape carries its size, long side first; every other listing none
    assert [(r.free_size_length, r.free_size_width) for r in rows] == [
        (None, None), (0.4, 0.4), (0.8, 0.4), (None, None)]


@pytest.mark.parametrize("row", [
    _tile(dims="length=60cm;height=40cm"),                  # no free side
    _tile(fmt="slab"),                                       # a category with no shapes
    _tile(dims="length=Free"),                               # the other side not stated
    _tile(dims="length=Free;height=130cm"),                  # even the square is over the cap
], ids=["sized", "slab", "other side missing", "over the cap"])
def test_a_listing_with_no_offerable_shape_passes_through_unchanged(ref, row):
    rows, fanned = free_size.run([row], ref)
    assert fanned == 0 and rows == [row] and rows[0] is row
    assert not rows[0].is_free_size_shape


def test_the_free_word_is_read_case_and_space_insensitive(ref):
    rows, fanned = free_size.run([_tile(dims="length= FREE ;height=40cm")], ref)
    assert fanned == 1 and len(rows) == 2


def test_a_shape_key_that_meets_another_listing_is_a_hard_error(ref):
    with pytest.raises(KeyCollisionError, match="MAR-P/12/03-40X40"):
        free_size.run([_tile(), _tile(key="mar-p/12/03-40x40", dims="length=60cm;height=40cm")], ref)


# --- size, stock, validation ----------------------------------------------------------------------------

def _derived(rows, ref, cfg):
    for row in rows:
        derive._derive_row(row, ref, cfg)
    return rows


@pytest.mark.parametrize("dims", ["length=Free;height=40cm", "length=40cm;height=Free"])
def test_each_shape_carries_its_size_long_side_in_length(ref, cfg, dims):
    rows, _ = free_size.run([_tile(dims=dims)], ref)
    _derived(rows, ref, cfg)
    assert [(r.length, r.width, r.height) for r in rows] == [(0.4, 0.4, 0.018), (0.8, 0.4, 0.018)]
    for r in rows:
        methods = r.dimension_method.split(",")
        assert "length:free_size_shape" in methods and "width:free_size_shape" in methods
        assert not any(f.code == FlagCode.dimension_defaulted for f in r.review_flags)
    assert len({r.handle for r in rows}) == 2


def test_a_tile_free_on_every_side_gets_both_offered_sides(ref, cfg):
    rows, _ = free_size.run([_tile(dims="length=Free;height=Free")], ref)
    _derived(rows, ref, cfg)
    assert [(r.length, r.width) for r in rows] == [(0.6, 0.6), (0.6, 0.3), (0.6, 0.15)]
    assert all("width:free_size_shape" in r.dimension_method.split(",") for r in rows)


def test_a_shape_is_made_to_order_whatever_area_the_vendor_publishes(ref, cfg):
    rows, _ = free_size.run([_tile(raw_stock_m2="120", raw_stock_status="in-stock")], ref)
    _derived(rows, ref, cfg)
    for r in rows:
        assert r.made_to_order is True
        assert r.inventory_quantity is None
        assert r.inventory_method == "made_to_order"
        validate.validate_row(r, source_code="test")
        assert not any(x.rule == "stock_undetermined" for x in r.reject_reasons)


def test_a_vendor_out_of_stock_still_wins_over_made_to_order(ref, cfg):
    rows, _ = free_size.run([_tile(raw_stock_status="out-of-stock")], ref)
    _derived(rows, ref, cfg)
    assert all(r.inventory_quantity == 0 and r.made_to_order is False for r in rows)
