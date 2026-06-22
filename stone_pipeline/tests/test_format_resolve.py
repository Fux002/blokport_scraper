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
