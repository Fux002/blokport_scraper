"""Format resolution: block / slab / tile must be explicit and provenance-bearing.

The format should come from a scraper-level tag; the resolver trusts that first,
then a format word in the name, then a clean structural inference, and otherwise
flags it unresolved rather than guessing.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import format_resolve
from stone_pipeline.state.overrides import Overrides


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _resolve(ref, **kw):
    row = CanonicalRow(src_site="x", surrogate_key="1", **kw)
    format_resolve.resolve_format(row, ref)
    return row


def test_explicit_tag_wins(ref):
    for tag, is_block in [("Block", True), ("Slab", False), ("Tile", False)]:
        row = _resolve(ref, raw_format=tag)
        assert row.format_value == tag
        assert row.is_block is is_block
        assert row.format_method == "explicit_tag"
        assert row.format_confidence == "high"


def test_plural_tag_canonicalises_to_singular(ref):
    # L#9: a scraper may tag the format in the plural. category() accepts it, but format_value must be the
    # canonical SINGULAR -- else the dimension bucket ('tile') and is_block ('block') comparisons miss, so a
    # 'Tiles' row gets slab dims/weight and a 'Blocks' row ships as a slab.
    for tag, expected, is_block in [("Tiles", "Tile", False), ("Blocks", "Block", True),
                                    ("slabs", "Slab", False)]:
        row = _resolve(ref, raw_format=tag)
        assert row.format_value == expected           # singular, never 'Tiles'/'Blocks'
        assert row.is_block is is_block               # a plural 'Blocks' still sets is_block
        assert row.format_method == "explicit_tag"


def test_name_word_when_no_tag(ref):
    row = _resolve(ref, raw_format="", raw_name="Pietra Gray Marble Slab")
    assert row.format_value == "Slab"
    assert row.format_method == "name_word"
    row_b = _resolve(ref, raw_format="", raw_name="Cream Marble Tile")
    assert row_b.format_value == "Tile"


def test_structural_infers_slab_and_flags(ref):
    # slabware indicators (slab count / area / thickness) -> Slab, flagged
    row = _resolve(ref, raw_format="", raw_name="ALPINE", raw_slab_count="10", raw_thickness="2cm")
    assert row.format_value == "Slab"
    assert row.format_method == "structural"
    assert any(f.code == FlagCode.format_inferred for f in row.review_flags)


def test_structural_thick_depth_infers_block_not_slab(ref):
    # the MAR-682 bug: no tag, no name word, but a 2 m depth in the thickness field. Presence of a thickness
    # must NOT default to slab -- a block-scale depth resolves to Block (and derive then keeps the real depth
    # instead of clamping it to the 2 cm slab default).
    row = _resolve(ref, raw_format="", raw_name="Marjan Silver Travertine No. 682", raw_thickness="200cm")
    assert row.format_value == "Block"
    assert row.is_block is True
    assert row.format_method == "structural"
    assert any(f.code == FlagCode.format_inferred for f in row.review_flags)


def test_structural_thin_depth_infers_slab(ref):
    # a slab-scale thickness with no other signal still resolves to Slab (the common untagged slab case).
    row = _resolve(ref, raw_format="", raw_name="Some Marble", raw_thickness="2cm")
    assert row.format_value == "Slab"
    assert row.format_method == "structural"


def test_structural_ambiguous_depth_declines_to_unresolved(ref):
    # a depth in neither the slab band nor the block band is not guessed -- it declines to the flagged
    # unresolved fallback rather than forcing a possibly-wrong format.
    row = _resolve(ref, raw_format="", raw_name="Mystery", raw_thickness="20cm")
    assert row.format_method == "unresolved_default"
    assert any(f.code == FlagCode.format_unresolved for f in row.review_flags)


def test_unresolved_is_flagged_not_guessed(ref):
    # no tag, no name word, no structural signal -> flagged, slab branch fallback
    row = _resolve(ref, raw_format="", raw_name="Mystery Product")
    assert row.format_method == "unresolved_default"
    assert row.format_confidence == "none"
    assert any(f.code == FlagCode.format_unresolved for f in row.review_flags)
    assert row.is_block is False  # falls back to the slab branch so the run continues


def test_override_beats_everything(ref):
    ref.overrides = Overrides(by_key={("x", "1"): {"format_value": "block"}})
    try:
        row = _resolve(ref, raw_format="Slab", raw_name="X Slab", raw_slab_count="5")
        assert row.format_value == "Block"
        assert row.is_block is True
        assert row.format_method == "override"
    finally:
        ref.overrides = Overrides()


def test_block_tag_selects_block_branch_and_no_bundle(ref):
    # a real block tag must route to the block branch (Blocks pcat downstream)
    row = _resolve(ref, raw_format="Block")
    assert row.is_block is True
