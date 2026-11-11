"""Brazilian White and Super White are DIFFERENT stones (owner, 11-11): 641ae4c's merge is undone in the candidate's
committed data, on the OLD Keys (PLAN_brazilian_white_undo.md section 3, tests section 5). Brazilian White is its own
variety in every fan-out category for Marble, Onyx and Quartzite; the real Super White Quartzite (Fjord White
Quartzite) is its own quartzite; Super White Dolomite is unchanged; no alias binds one stone's listing to the other;
the key map gives the four identities with one rank-0 copy each for the two quartzites; and a renamed variety
re-serves under its old Key, so the Pull renames it in Medusa. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import populate, sync
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.phase1 import keymap
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation

REPO = Path(__file__).resolve().parents[2]
ENVS = ("development", "production")
BASES = {env: REPO / "from_medusa" / env / "variants_export_base.csv" for env in ENVS}
BACKBONES = [REPO / "catalog_source" / f"backbone_{c}.json" for c in ("slabs", "blocks", "tiles")]
ORIGIN_MAP = REPO / "catalog_source" / "origin_map.csv"
ATTRIBUTES = [REPO / "from_medusa" / "development" / "attributes.csv"]

BW, SW = "Brazilian White", "Super White"
# old Key -> (type, the Name it carries after the undo)
OLD_KEYS = {
    "slab_marble_brazilian_white_b1cfd99b-9604-4bab-af4e-a990d725ad36": ("Marble", BW),
    "block_marble_super_white_131d09ad-addf-5ce6-a46a-c66ca1efc893": ("Marble", BW),
    "tile_marble_super_white_bea4f3ba-a202-540e-9922-5722e8124efa": ("Marble", BW),
    "slab_onyx_brazilian_white_fecf8ec4-53bf-4f1b-97a5-b5514ac16237": ("Onyx", BW),
    "block_onyx_super_white_131d09ad-addf-5ce6-a46a-c66ca1efc893": ("Onyx", BW),
    "tile_onyx_super_white_bea4f3ba-a202-540e-9922-5722e8124efa": ("Onyx", BW),
    "slab_quartzite_brazilian_white_f514e927-983d-50ae-9aaa-98118cdea823": ("Quartzite", BW),
    "block_quartzite_brazilian_white_da310347-aa7b-52a5-8d26-0bee1f4a71cb": ("Quartzite", BW),   # union fill
    "tile_quartzite_brazilian_white_73bc5156-49fe-54f3-a1c2-3d7c1da75008": ("Quartzite", BW),    # union fill
    "slab_quartzite_super_white_c84613a4-eae0-5fe8-920f-1b63b5bfe4e6": ("Quartzite", SW),        # union fill
    "block_quartzite_super_white_131d09ad-addf-5ce6-a46a-c66ca1efc893": ("Quartzite", SW),
    "tile_quartzite_super_white_bea4f3ba-a202-540e-9922-5722e8124efa": ("Quartzite", SW),
}
FILLS = {"block_quartzite_brazilian_white_da310347-aa7b-52a5-8d26-0bee1f4a71cb",
         "tile_quartzite_brazilian_white_73bc5156-49fe-54f3-a1c2-3d7c1da75008",
         "slab_quartzite_super_white_c84613a4-eae0-5fe8-920f-1b63b5bfe4e6"}
SUPER_WHITE_NAMES = {"super white", "super white marble", "super white quartzite", "fjord white quartzite",
                     "white super", "white super es"}
BRAZILIAN_WHITE_NAMES = {"brazilian white", "brazilian white quartzite", "brazil arabescato",
                         "brazil arabescato marble", "yudu hui"}


def _base(env):
    with BASES[env].open(encoding="utf-8-sig", newline="") as h:
        return {r["Key"]: r for r in csv.DictReader(h)}


def _aliases(text):
    return {a.strip().lower() for a in (text or "").split("|") if a.strip()}


@pytest.mark.parametrize("env", ENVS)
def test_every_old_key_carries_its_stone_in_the_base(env):
    rows = _base(env)
    for key, (stone_type, name) in OLD_KEYS.items():
        assert key in rows, key
        assert rows[key]["Name"] == name, key
        forbidden = SUPER_WHITE_NAMES if name == BW else BRAZILIAN_WHITE_NAMES
        assert not _aliases(rows[key]["Aliases"]) & forbidden, key
    for branch in ("slab", "block", "tile"):          # union-uniform: each identity in every fan-out category
        for stone_type in ("Marble", "Onyx", "Quartzite"):
            assert any(k.startswith(f"{branch}_{stone_type.lower()}_") and n == (stone_type, BW)
                       for k, n in OLD_KEYS.items()), (branch, stone_type)


def test_every_backbone_post_holds_its_own_stone():
    for path in BACKBONES:
        posts = json.loads(path.read_text(encoding="utf-8"))["posts"]
        mine = [p for p in posts if p["stone_type"] in ("Marble", "Onyx", "Quartzite")
                and p["variant"] in (BW, SW)]
        assert {(p["stone_type"], p["variant"]) for p in mine} == {("Marble", BW), ("Onyx", BW), ("Quartzite", BW),
                                                                   ("Quartzite", SW)}, path.name
        for p in mine:
            forbidden = SUPER_WHITE_NAMES if p["variant"] == BW else BRAZILIAN_WHITE_NAMES
            assert not {a.lower() for a in p["aliases"]} & forbidden, (path.name, p["variant"], p["stone_type"])


def test_each_identity_has_its_origin_row():
    with ORIGIN_MAP.open(encoding="utf-8-sig", newline="") as h:
        rows = {(r["variety"], r["stone_type"]) for r in csv.DictReader(h)}
    for identity in [(BW, "Marble"), (BW, "Onyx"), (BW, "Quartzite"), (SW, "Quartzite"), (SW, "Dolomite")]:
        assert identity in rows, identity
    assert (SW, "Marble") not in rows and (SW, "Onyx") not in rows


def test_the_key_map_gives_the_four_identities_with_one_rank0_copy_per_quartzite():
    records = []
    for env in ENVS:
        records += [r for r in keymap.base_csv_records(BASES[env], env) if r.old_key in OLD_KEYS]
        records += [r for r in keymap.backbone_records(BACKBONES, env) if r.old_key in OLD_KEYS]
    result = keymap.build_key_map(records, keymap.load_type_vocab(ATTRIBUTES))
    assert result.aborts == {}
    mapped = {row.old_key: row for row in result.rows}
    assert set(mapped) == set(OLD_KEYS)
    for key, (stone_type, name) in OLD_KEYS.items():
        assert mapped[key].new_key == new_key(stone_type, name), key
    new_keys = {row.new_key for row in result.rows}
    assert new_key("Marble", SW) not in new_keys and new_key("Onyx", SW) not in new_keys
    for name in (BW, SW):                              # fd's gate: each quartzite keeps ONE rank-0 copy, its slab
        ranks = sorted(r.category_rank for r in result.rows if r.new_key == new_key("Quartzite", name))
        assert ranks == [0, 1, 2], name


def test_a_renamed_variety_re_serves_under_its_old_key(tmp_path):
    # fd's gate: P1 takes each identity's name from its survivor, so every live Brazilian White Key must be renamed
    # in Medusa first. The base's new Name changes the payload, so the row re-serves under the SAME old Key.
    rows = _base("development")
    live = [k for k in OLD_KEYS if k not in FILLS]
    before, after = tmp_path / "before.csv", tmp_path / "after.csv"
    for path, rename in ((before, True), (after, False)):
        with path.open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h)
            w.writerow(list(populate._VARIANTS_FULL_COLS))
            for k in live:
                r = rows[k]
                w.writerow([k, SW if rename else r["Name"], r["Image"], r["Aliases"], r["Volume per kg (m³/kg)"]])
    with Ledger.open(tmp_path / "t.ledger", env="development") as led:
        for _ in range(2):              # the second pass hashes the typed rows, so only the rename can move a hash
            populate.populate_variations_full(led, before)
            for k in live:
                led.execute("UPDATE variation SET state = 'synced', medusa_id = ?, type = ? WHERE key = ?",
                            (f"m-{k}", OLD_KEYS[k][0], k))
        populate.populate_variations_full(led, after)
        served = {v["external_id"]: v for v in sync.ready(led, "variations")}
    renamed = [k for k in live if OLD_KEYS[k][1] == BW]
    assert set(renamed) <= set(served)
    for k in renamed:
        assert served[k]["payload"]["name"] == BW, k


@pytest.fixture(scope="module")
def stage():
    return match_variation.VariationStage.build(loaders.load_all())


def _bind(stage, src_site, name, stone_type):
    row = CanonicalRow(src_site=src_site, surrogate_key="x", format_value="Slab", variety_match_key=name,
                       raw_type=stone_type, raw_color="White")
    stage.resolve_row(row)
    return row.variation_key


def test_each_listing_binds_its_own_stone(stage):
    # marenostone P/06/22 "Brazilian White Quartzite Slab" and polonine "FJORD WHITE QUARTZITE" (fixtures
    # data/marenostone/20260710_155335, data/polonine/20260619_214426) are two stones
    assert _bind(stage, "marenostone", "Brazilian White Quartzite", "Quartzite") == \
        "slab_quartzite_brazilian_white_f514e927-983d-50ae-9aaa-98118cdea823"
    assert _bind(stage, "polonine", "Fjord White Quartzite", "Quartzite") == \
        "slab_quartzite_super_white_c84613a4-eae0-5fe8-920f-1b63b5bfe4e6"
    assert _bind(stage, "polonine", "Super White", "Dolomite") == \
        "slab_dolomite_super_white_c84613a4-eae0-5fe8-920f-1b63b5bfe4e6"


def test_a_super_white_marble_listing_never_binds_brazilian_white(stage):
    brazilian = _bind(stage, "marenostone", BW, "Marble")
    assert brazilian == "slab_marble_brazilian_white_b1cfd99b-9604-4bab-af4e-a990d725ad36"
    assert _bind(stage, "marenostone", SW, "Marble") != brazilian
