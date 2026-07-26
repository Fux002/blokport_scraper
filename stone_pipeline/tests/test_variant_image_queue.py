"""Variant image queue: every category variant of a STOCKED variety gets its OWN image.

A variety is stocked when any of its category keys is product-backed (products reference the slab). Then
EACH of its slab/block/tile variants needs its own image, generated from that category's base image (a tile
is NOT a copy of the slab -- different base, different render). Keying the backfill on the product-backed
KEY missed the tile/block (they aren't themselves product-backed), which is why they were blank.
"""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace

from stone_pipeline.stages import image_prompts


def test_build_queues_every_category_of_a_stocked_variety_from_its_own_base(tmp_path, monkeypatch):
    (tmp_path / "backbone_additions").mkdir()                 # empty additions -> only backfill drives it
    exp = tmp_path / "export.csv"
    # build() reads only these two paths in the backfill; SETTINGS is a frozen dataclass, so stub the
    # module-level reference the function actually uses (product_backed_keys/_backbone_types are stubbed too).
    monkeypatch.setattr(image_prompts, "SETTINGS",
                        SimpleNamespace(paths=SimpleNamespace(catalog_source_dir=tmp_path, export_file=exp)))
    with exp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=["Key", "Name", "Image"])
        w.writeheader()
        w.writerow({"Key": "slab_marble_arabescato_A", "Name": "Arabescato", "Image": "http://s.png"})  # imaged
        w.writerow({"Key": "block_marble_arabescato_B", "Name": "Arabescato", "Image": ""})   # blank -> queue
        w.writerow({"Key": "tile_marble_arabescato_C", "Name": "Arabescato", "Image": ""})    # blank -> queue
        w.writerow({"Key": "tile_marble_ghost_D", "Name": "Ghost", "Image": ""})              # not stocked -> skip
    monkeypatch.setattr(image_prompts, "product_backed_keys", lambda: {"slab_marble_arabescato_A"})
    monkeypatch.setattr(image_prompts, "_backbone_types", lambda: {
        "slab_marble_arabescato_A": "Marble", "block_marble_arabescato_B": "Marble",
        "tile_marble_arabescato_C": "Marble", "tile_marble_ghost_D": "Marble"})

    out = image_prompts.build(out_path=tmp_path / "q.json")
    items = json.loads(out.read_text(encoding="utf-8"))
    by = {i["output_name"]: i for i in items}

    assert "block_marble_arabescato_B" in by                  # stocked variety's blank block -> queued
    assert "tile_marble_arabescato_C" in by                   # stocked variety's blank tile  -> queued
    assert "slab_marble_arabescato_A" not in by               # already imaged -> not queued
    assert "tile_marble_ghost_D" not in by                    # variety not stocked -> not queued
    # each queued variant carries ITS OWN category base image (tile base != block base): a real render, not a copy
    assert by["tile_marble_arabescato_C"]["base_image_url"] != by["block_marble_arabescato_B"]["base_image_url"]
