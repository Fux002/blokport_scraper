"""MB-1 (570's DoD maintenance row; the scraper manager, 12 Nov): a plan face longer than any real one is a data error,
never published. Dev's polonine POL-755 'Alpine' listed a 19.75 m slab (3.27 t), and zucchi's Misty Black
BD00145610/12/13 6.44 x 3.64 m slabs: the range check only flagged softly (and its 9 m slab ceiling let 6.44 m pass).

The pack states each category's face maximum (stone.yaml `face_max`, metres, beside dimension_ranges), measured on the
real data: on the dev ledger (12 Nov 17:09Z) the longest real slab face is 3.534 m (p99.9 3.528, n=5,469), block 3.53
(n=78), tile 1.45 (n=136); each maximum is that plus ~7.5%: slab and block 3.80, tile 1.60. derive flags a face over it
(dimension_implausible, per face, after the faces are ordered); validate holds the row under the rule of that name,
listed or not (the value is wrong, so a published listing is held too).
"""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config import domain
from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive
from stone_pipeline.stages.validate import validate_row
from stone_pipeline.tests.test_domains import _valid_pack_dict, _write_pack


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _row(raw_format: str, raw_dimensions: str, raw_thickness: str = "2cm") -> CanonicalRow:
    return CanonicalRow(src_site="polonine", surrogate_key="755", raw_format=raw_format, variation_name="Alpine",
                        finish_name="Polished", type_name="Granite", color_name="White",
                        type_method="variety_authoritative", raw_dimensions=raw_dimensions, raw_thickness=raw_thickness)


def _derive(ref, row):
    ref.overrides = None
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    return row


def _implausible(row) -> dict[str, str]:
    return {f.field: f.detail for f in row.review_flags if f.code == FlagCode.dimension_implausible}


# --- the pack ---------------------------------------------------------------------------------------------------

def test_the_stone_pack_states_each_face_maximum_from_the_measured_data():
    assert domain.load_pack("stone").face_max == {"slab": 3.8, "block": 3.8, "tile": 1.6}


@pytest.mark.parametrize("face_max, match", [
    ({"nope": 3.8}, "not in the declared categories"),
    ({"x": 0}, "must be a positive number"),
    ({"x": True}, "must be a positive number"),
    ([3.8], "must be a mapping of category -> metres"),
])
def test_a_malformed_face_max_fails_loud_at_load(tmp_path, monkeypatch, face_max, match):
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["face_max"] = face_max
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match=match):
        domain.load_pack("bad")


# --- derive flags the face --------------------------------------------------------------------------------------

def test_the_19_75_m_alpine_slab_is_flagged_implausible_on_its_long_face(ref):
    row = _derive(ref, _row("Slab", "length=1975cm;height=307cm"))
    assert (row.length, row.width) == (19.75, 3.07)
    assert _implausible(row) == {"length": "face max 3.8 m (slab)"}


def test_the_6_44_by_3_64_m_slab_is_flagged_on_both_faces(ref):
    row = _derive(ref, _row("Slab", "length=644cm;height=364cm"))
    assert set(_implausible(row)) == {"length"}             # width 3.64 is under 3.80; only the 6.44 face is wrong
    row = _derive(ref, _row("Slab", "length=644cm;height=390cm"))
    assert set(_implausible(row)) == {"length", "width"}


def test_the_longest_real_slab_and_the_maximum_itself_are_kept(ref):
    assert _implausible(_derive(ref, _row("Slab", "length=353cm;height=205cm"))) == {}
    assert _implausible(_derive(ref, _row("Slab", "length=380cm;height=205cm"))) == {}
    assert set(_implausible(_derive(ref, _row("Slab", "length=381cm;height=205cm")))) == {"length"}


def test_a_tile_is_held_to_the_tile_maximum(ref):
    assert _implausible(_derive(ref, _row("Tile", "length=160cm;height=60cm"))) == {}
    assert _implausible(_derive(ref, _row("Tile", "length=161cm;height=60cm"))) == {"length": "face max 1.6 m (tile)"}


def test_the_maximum_is_read_from_the_pack(ref, monkeypatch):
    pack = derive.active_pack()
    narrower = dataclasses.replace(pack, face_max={**pack.face_max, "slab": 2.5})
    monkeypatch.setattr(derive, "active_pack", lambda: narrower)
    assert _implausible(_derive(ref, _row("Slab", "length=300cm;height=200cm"))) == {"length": "face max 2.5 m (slab)"}


# --- validate holds it, listed or not ---------------------------------------------------------------------------

def _flagged() -> CanonicalRow:
    r = CanonicalRow(src_site="polonine", surrogate_key="755", company_id="co_1", sales_channel_id="sc_1")
    r.length, r.width, r.height, r.inventory_quantity = 19.75, 3.07, 0.02, 1
    r.add_flag(ReviewFlag(field="length", code=FlagCode.dimension_implausible, raw_value="19.75",
                          confidence=Confidence.low, method="face_max", detail="face max 3.8 m (slab)"))
    return r


def test_validate_holds_an_implausible_face_under_its_own_rule():
    r = _flagged()
    validate_row(r, source_code="POL")
    assert [(x.rule, x.detail) for x in r.reject_reasons if x.rule == "dimension_implausible"] == \
        [("dimension_implausible", "length")]


def test_a_published_listing_is_held_too():
    r = _flagged()
    validate_row(r, source_code="POL", published=frozenset({"POL-755"}))
    assert any(x.rule == "dimension_implausible" for x in r.reject_reasons)
