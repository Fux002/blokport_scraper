"""Variant image queue: a texture is queued ONLY for a variant that has a PRODUCT to list.

New model (replacing the old whole-catalog backfill): a variety is prepared in Medusa for all categories,
but a category's texture is generated only when a product for that variant+category is scraped (the
backbone delta marks it product_backed). A fan-out category with no product is skipped -- it stays an
imageless record (Blokport placeholders it) until its own product appears. No pre-texturing of block/tile
for a variety only ever sold as slabs.
"""

from __future__ import annotations

import json

from stone_pipeline.stages import image_prompts


def test_build_queues_only_product_backed_not_fanout_categories(tmp_path):
    add = tmp_path / "backbone_additions"
    add.mkdir()
    (add / "delta.json").write_text(json.dumps([
        {"key": "slab_marble_new_stone_a1b2", "stone_type": "Marble", "variant": "New Stone",
         "product_backed": True},                                   # product for the slab -> queue its texture
        {"key": "block_marble_new_stone_c3d4", "stone_type": "Marble", "variant": "New Stone",
         "product_backed": False},                                  # fan-out, no product -> NOT queued
        {"key": "tile_marble_new_stone_e5f6", "stone_type": "Marble", "variant": "New Stone",
         "product_backed": False},                                  # fan-out, no product -> NOT queued
    ]), encoding="utf-8")

    out = image_prompts.build(additions_dir=add, out_path=tmp_path / "q.json")
    keys = {i["output_name"] for i in json.loads(out.read_text(encoding="utf-8"))}

    assert "slab_marble_new_stone_a1b2" in keys        # product-backed -> its texture is generated
    assert "block_marble_new_stone_c3d4" not in keys   # no product for this category -> imageless record, not queued
    assert "tile_marble_new_stone_e5f6" not in keys
