"""Tiles are a category of the ONE variety record (phase 1: a variety is one record per (type, name), with no
category of its own). A tile row resolves against the same varieties as a slab or block row, under the same id, and
matches the same way while the tile category is not activated: validate refuses it then (category_invalid, its pcat
is no active one), so it never emits.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation, validate


@pytest.fixture
def ref():
    return loaders.load_all()


def _deactivate_tiles(monkeypatch):
    import dataclasses
    from stone_pipeline.config import categories
    monkeypatch.setitem(categories._BY_NAME, "tile",
                        dataclasses.replace(categories._BY_NAME["tile"], pcat_id=""))


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


def test_a_tile_row_of_an_inactive_category_matches_and_validate_refuses_it(ref, monkeypatch):
    _deactivate_tiles(monkeypatch)                       # the tile category has no Medusa category id yet
    stage = match_variation.VariationStage.build(ref)
    slab = CanonicalRow(src_site="polonine", surrogate_key="s1", format_value="Slab", variety_match_key="Arabescato",
                        raw_type="Marble", raw_color="White")
    tile = CanonicalRow(src_site="polonine", surrogate_key="t1", format_value="Tile", variety_match_key="Arabescato",
                        raw_type="Marble", raw_color="White", category_pcat_id="")
    stage.resolve_row(slab)
    stage.resolve_row(tile)
    assert tile.variation_key == slab.variation_key      # no other category's id to borrow: it is the same variety
    validate.validate_row(tile, source_code="pol")
    assert "category_invalid" in {r.rule for r in tile.reject_reasons}
