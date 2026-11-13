"""SC-4 2g (plan SC-3-SC-4-PLAN.md 2g; spec rev 8 section 3 "Backbone result", D7, D9): ONE variety backbone.

- `catalog_source/backbone_varieties.json` replaces the three per-category backbones: one post per (type, name) on
  its phase 1 Key, carrying the slab post's finishes and qualities and the union of every copy's colours and
  aliases. Every post maps through SC-1's key map (f0e18346) except the one named mint (manager, 10 Nov).
- The committed additions are ONE `backbone_additions/varieties.json`: each post keyed, with the sorted
  `categories` its copies sat in; an untyped post has no Key and is HELD (skipped by the one additions reader).
- The pack: one pack-level backbone file; `mirror_of` becomes `finishes_from` and an unknown category key is
  refused; every non-default category declares `finishes` or `finishes_from`; a declared finish that does not
  resolve fails loud.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stone_pipeline.config import domain
from stone_pipeline.config.categories import CATEGORIES, category
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.phase1 import rewrite_committed
from stone_pipeline.phase1.migrate_stores import read_key_map
from stone_pipeline.phase1.variety_key import variety_key_problem
from stone_pipeline.reference.loaders import backbone as backbone_loader
from stone_pipeline.stages import tree_build

CATALOG = rewrite_committed.REPO_ROOT / "catalog_source"
VARIETIES = CATALOG / "backbone_varieties.json"
ADDITIONS = CATALOG / "backbone_additions" / "varieties.json"
# P2-10's seed rewrite adds `codes` to every backbone post (the seed store's code set); curate's additions keep theirs
ADDITION_FIELDS = {"key", "variant", "stone_type", "color", "finishes", "qualities", "aliases", "merged_from",
                   "categories"}
POST_FIELDS = ADDITION_FIELDS - {"categories"} | {"codes"}
# D7: block's own finish set, as the finish vocabulary spells it
BLOCK_FINISHES = ["Brushed", "Chain Sawn & Squared", "Chain Sawn & Un-squared", "Diamond Wire Sawn & Squared",
                  "Diamond Wire Sawn & Un-squared", "Drill, Split & Squared", "Drill, Split & Un-squared",
                  "Flame Jet & Squared", "Flame Jet & Un-squared", "Flamed", "Honed", "Leathered", "Polished", "Raw",
                  "Sandblasted", "Sawn Cut", "Water Jet Cut & Squared", "Water Jet Cut & Un-squared"]


def _posts(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["posts"] if isinstance(doc, dict) else doc      # a backbone is {"posts": [...]}, additions a list


# --- the committed data ---------------------------------------------------------------------------------------------

def test_one_variety_backbone_replaces_the_three():
    assert VARIETIES.exists()
    gone = ["backbone_slabs.json", "backbone_blocks.json", "backbone_tiles.json", "image_model.csv",
            "missing_variants.csv", "backbone_additions/slab.json", "backbone_additions/block.json",
            "backbone_additions/tile.json"]
    assert [name for name in gone if (CATALOG / name).exists()] == []


def test_every_post_is_one_identity_on_its_phase1_key():
    posts = _posts(VARIETIES)
    assert [p["key"] for p in posts if variety_key_problem(p["key"])] == []
    assert len({p["key"] for p in posts}) == len(posts)
    assert len({(p["stone_type"].casefold(), p["variant"].casefold()) for p in posts}) == len(posts)
    assert [p["key"] for p in posts if set(p) != POST_FIELDS] == []


def test_every_post_maps_through_the_key_map():
    # the map is not in CI: the rewrite refuses any post the map does not carry when it runs (nothing is minted),
    # and this pins the Keys it wrote
    assert rewrite_committed.mapped_keys_sha256(_posts(VARIETIES)) == rewrite_committed.MAPPED_BACKBONE_KEYS_SHA256


def test_the_committed_additions_are_one_file_keyed_or_held():
    names = {c.name for c in CATEGORIES}
    posts = _posts(ADDITIONS)
    assert posts and [p.get("key") for p in posts if set(p) != ADDITION_FIELDS] == []
    for p in posts:
        assert p["categories"] == sorted(p["categories"]) and set(p["categories"]) <= names
        assert (variety_key_problem(p["key"]) is None) if p["key"] else (p["stone_type"] == "")


# --- the merge -------------------------------------------------------------------------------------------------------

_K = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666"
_MAP = ("old_key,new_key,category_rank,type_name\n"
        f"slab_granite_absolute_black_a,{_K},0,Granite\n"
        f"block_granite_absolute_black_b,{_K},1,Granite\n"
        f"tile_granite_absolute_black_c,{_K},2,Granite\n").encode()


@pytest.fixture
def key_map():
    return read_key_map(_MAP, hashlib.sha256(_MAP).hexdigest())


def _post(**kw):
    base = dict(variant="Absolute Black", stone_type="Granite", color=["Black"], finishes=["Polished"],
                qualities=["A"], aliases=[], merged_from=[], category="Slabs", image_file="x.png", exists="yes",
                in_csv="yes")
    base.update(kw)
    return base


def test_an_identitys_three_posts_merge_into_one(key_map):
    slab = _post(key="slab_granite_absolute_black_a", aliases=["Shanxi Black"], finishes=["Polished", "Honed"])
    block = _post(category="Blocks", color=["Black", "Grey"], finishes=["Raw"], qualities=["B"],
                  aliases=["shanxi black", "Nero Assoluto"])                       # keyless: joins by (type, name)
    tile = _post(key="tile_granite_absolute_black_c", category="Tiles", aliases=["Tile Black"])
    (post,) = rewrite_committed.merge_backbones([slab], [block], [tile], key_map)
    assert post == {"key": _K, "variant": "Absolute Black", "stone_type": "Granite", "color": ["Black", "Grey"],
                    "finishes": ["Polished", "Honed"], "qualities": ["A"],            # the slab post's (D7)
                    "aliases": ["Shanxi Black", "Nero Assoluto", "Tile Black"], "merged_from": []}


def test_a_post_the_map_does_not_carry_refuses(key_map):
    with pytest.raises(rewrite_committed.UnmappedKey, match="Kashmir White"):
        rewrite_committed.merge_backbones([], [_post(variant="Kashmir White", category="Blocks")], [], key_map)


def test_the_merge_is_idempotent(key_map):
    once = rewrite_committed.merge_backbones([_post(key="slab_granite_absolute_black_a")], [], [], key_map)
    assert rewrite_committed.merge_backbones(once, [], [], key_map) == once


# --- the pack (D7, D9) and the readers -------------------------------------------------------------------------------

def test_the_pack_names_one_backbone_file():
    assert SETTINGS.paths.backbone_json == VARIETIES
    assert not hasattr(category("slab"), "backbone_path")


def test_a_leftover_mirror_of_fails_loud(tmp_path, monkeypatch):
    text = domain._pack_path("stone").read_text(encoding="utf-8").replace("finishes_from: slab", "mirror_of: slab", 1)
    (tmp_path / "stone.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    with pytest.raises(ValueError, match="mirror_of"):
        domain.load_pack("stone")


def test_stones_categories_declare_their_finish_sets():
    assert category("tile").finishes_from == "slab" and category("block").finishes == tuple(BLOCK_FINISHES)
    assert category("slab").finishes == () and category("slab").finishes_from is None


def test_the_category_finish_sets_follow_the_pack():
    attr = tree_build._load_attributes(SETTINGS.paths.attributes_csv)
    sets = tree_build._category_finishes([VARIETIES], attr, {})
    assert sets["tile"] == sets["slab"]
    assert sets["block"] == sorted(attr["finish"][tree_build.match_key(f)] for f in BLOCK_FINISHES)


def test_a_declared_finish_that_does_not_resolve_fails_loud(monkeypatch):
    import dataclasses
    attr = tree_build._load_attributes(SETTINGS.paths.attributes_csv)
    cats = tuple(dataclasses.replace(c, finishes=("Moon Polished",)) if c.name == "block" else c for c in CATEGORIES)
    monkeypatch.setattr(tree_build, "CATEGORIES", cats)
    with pytest.raises(ValueError, match="Moon Polished"):
        tree_build._category_finishes([VARIETIES], attr, {})


def test_a_held_additions_post_is_skipped_by_the_one_reader(tmp_path):
    path = tmp_path / "varieties.json"
    path.write_text(json.dumps([
        {"key": "", "variant": "Avocatus", "stone_type": "", "categories": ["slab"]},
        {"key": _K, "variant": "Absolute Black", "stone_type": "Granite", "categories": ["slab"]}]),
        encoding="utf-8")
    assert [p["variant"] for p in backbone_loader.read_posts(path)] == ["Absolute Black"]
