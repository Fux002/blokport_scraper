"""Tiles are a category of the ONE variety record (phase 1: a variety is one record per (type, name), with no
category of its own). A tile row resolves against the same varieties as a slab or block row, under the same id. While
the tile category is not activated (no Medusa category id), a tile row is held in the gap queue and never resolved.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation, reconcile_tree


@pytest.fixture
def ref():
    return loaders.load_all()


def _deactivate_tiles(monkeypatch):
    import dataclasses
    from stone_pipeline.config import categories
    monkeypatch.setitem(categories._BY_NAME, "tile",
                        dataclasses.replace(categories._BY_NAME["tile"], pcat_id=""))


def test_a_tile_row_is_held_while_the_tile_category_is_not_activated(ref, monkeypatch):
    _deactivate_tiles(monkeypatch)  # category not wired up: tile held, never resolved
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="t1",
                       format_value="Tile", variety_match_key="White Travertine",
                       raw_name="White Travertine Tile", raw_type="Travertine")
    stage.resolve_row(row)
    assert row.variation_id is None  # never borrows a slab id
    assert row.variation_method == "category_not_activated"
    assert any(g.gap_kind == GapKind.missing_mirror_reference for g in row.tree_gaps)


def test_tile_does_not_double_gap_in_reconcile(ref, monkeypatch):
    _deactivate_tiles(monkeypatch)
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="t2",
                       format_value="Tile", variety_match_key="Cream Marble",
                       raw_name="Cream Marble Tile")
    stage.resolve_row(row)
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    kinds = [g.gap_kind for g in row.tree_gaps]
    assert GapKind.missing_mirror_reference in kinds
    assert GapKind.missing_variation not in kinds  # not double-gapped


def test_one_variety_has_one_id_in_every_category(ref):
    stage = match_variation.VariationStage.build(ref)
    rows = [CanonicalRow(src_site="polonine", surrogate_key=f"x{i}", format_value=fmt, variety_match_key="Arabescato",
                         raw_type="Marble", raw_color="White") for i, fmt in enumerate(("Slab", "Block", "Tile"))]
    for row in rows:
        stage.resolve_row(row)
    slab, block, tile = rows
    assert slab.variation_id and slab.variation_id == block.variation_id == tile.variation_id
    assert slab.variation_key == block.variation_key == tile.variation_key
    assert (slab.is_block, block.is_block, tile.is_block) == (False, True, False)
