"""Tiles are a distinct branch with their own variation ids. Until a tile_
variants export is supplied, tile rows are held in the gap queue and never given
a slab id. When tile variants exist, a tile resolves against them.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import Variant
from stone_pipeline.stages import match_variation, reconcile_tree


@pytest.fixture
def ref():
    return loaders.load_all()


def _deactivate_tiles(monkeypatch):
    import dataclasses
    from stone_pipeline.config import settings
    monkeypatch.setitem(settings._BY_NAME, "tile",
                        dataclasses.replace(settings._BY_NAME["tile"], pcat_id=""))


def _clear_tile_reference(ref):
    """The live export now contains tile variants; clear them to exercise the
    'no tile reference supplied yet' path (the function-scoped ref isolates this)."""
    ref.variants_tiles.by_id.clear()
    ref.variants_tiles.surface_to_id.clear()


def test_tile_with_no_reference_is_held_not_given_slab_id(ref, monkeypatch):
    _deactivate_tiles(monkeypatch)  # category not wired up: tile held, not given a slab id
    _clear_tile_reference(ref)
    assert len(ref.variants_tiles.by_id) == 0  # not supplied yet
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="t1",
                       format_value="Tile", variety_match_key="White Travertine",
                       raw_name="White Travertine Tile", raw_type="Travertine")
    stage.resolve_row(row)
    assert row.variation_id is None  # never borrows a slab id
    assert row.variation_method == "category_not_activated"
    assert any(g.gap_kind == GapKind.missing_tile_reference for g in row.tree_gaps)


def test_tile_does_not_double_gap_in_reconcile(ref, monkeypatch):
    _deactivate_tiles(monkeypatch)
    _clear_tile_reference(ref)
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="t2",
                       format_value="Tile", variety_match_key="Cream Marble",
                       raw_name="Cream Marble Tile")
    stage.resolve_row(row)
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    kinds = [g.gap_kind for g in row.tree_gaps]
    assert GapKind.missing_tile_reference in kinds
    assert GapKind.missing_variation not in kinds  # not double-gapped


def test_tile_resolves_when_tile_variants_supplied(ref):
    # simulate the tile export arriving with exactly one known tile variant
    _clear_tile_reference(ref)
    tid = "01TILEWHITETRAVERTINE00000"
    ref.variants_tiles.by_id[tid] = Variant(
        variation_id=tid, key="tile_travertine_white_travertine",
        name="White Travertine", image="", aliases=[])
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="t3",
                       format_value="Tile", variety_match_key="White Travertine",
                       raw_name="White Travertine Tile", raw_type="Travertine")
    stage.resolve_row(row)
    assert row.variation_id == tid  # resolves against the tile reference
    assert row.is_block is False


def test_slab_and_block_branches_unaffected(ref):
    stage = match_variation.VariationStage.build(ref)
    slab = CanonicalRow(src_site="polonine", surrogate_key="s1", format_value="Slab",
                        variety_match_key="Arabescato", raw_type="Marble", raw_color="White")
    block = CanonicalRow(src_site="x", surrogate_key="b1", format_value="Block",
                         variety_match_key="Arabescato", raw_type="Marble", raw_color="White")
    stage.resolve_row(slab)
    stage.resolve_row(block)
    assert slab.variation_id and slab.variation_id != block.variation_id  # different id per format
