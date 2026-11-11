"""SC-4 (Scraper's review of #623's golden, MEDIUM): a variety's backbone post is the one that carries its own Key.
In phase 1 every post carries its variety's Key, so the post is taken by Key; a name or alias lookup is only the
fallback for a variety with no keyed post. A name that is ALSO another post's alias must never resolve to that other
post because it happens to come first in the file (Silver Wave: its Name is no post's variant, two Marble posts carry
it as an alias, and the old file order hid the wrong pick). Reconcile's leaf membership and the matcher's colour
blocking both read the post this way.
"""
from __future__ import annotations

import json

from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference.loaders import Variant, VariantTable
from stone_pipeline.reference.loaders.backbone import load_backbone

OWN, OTHER = new_key("Marble", "Silver Wave"), new_key("Marble", "Persian Silver Wave")


def _backbone(tmp_path):
    posts = [  # the OTHER post first: file order must not decide
        {"key": OTHER, "variant": "Persian Silver Wave", "stone_type": "Marble", "color": ["Grey", "Silver", "Black"],
         "finishes": ["Polished"], "qualities": ["A"], "aliases": ["Silver Wave"]},
        {"key": OWN, "variant": "Silver Wave Silver Shadow", "stone_type": "Marble",
         "color": ["Grey", "Silver", "Brown", "Black"], "finishes": ["Polished"], "qualities": ["A"],
         "aliases": ["Silver Wave"]},
    ]
    path = tmp_path / "backbone_varieties.json"
    path.write_text(json.dumps({"posts": posts}), encoding="utf-8")
    return load_backbone(path)


def test_a_variety_s_post_is_the_one_carrying_its_own_key_whatever_the_file_order(tmp_path):
    variety = _backbone(tmp_path).variety_of(OWN, "Silver Wave", "Marble")
    assert variety.variant == "Silver Wave Silver Shadow" and "Brown" in variety.colors


def test_a_variety_with_no_keyed_post_falls_back_to_its_name(tmp_path):
    variety = _backbone(tmp_path).variety_of(new_key("Marble", "Unposted"), "Persian Silver Wave", "Marble")
    assert variety.variant == "Persian Silver Wave"


def test_the_matcher_blocks_colours_on_the_variety_s_own_post(tmp_path):
    table = VariantTable()
    table.by_id["v1"] = Variant(variation_id="v1", key=OWN, name="Silver Wave", aliases=[])
    index = build_variation_index(table, _backbone(tmp_path))
    assert proj.norm("Brown") in index.candidates["v1"].block_colors
