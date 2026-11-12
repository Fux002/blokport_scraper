"""Brazilian White and Super White are DIFFERENT stones (owner, 11-11): 641ae4c's merge is undone in the committed data
(PLAN_brazilian_white_undo.md section 4). Brazilian White is its own variety for Marble, Onyx and Quartzite; the real
Super White Quartzite (Fjord White Quartzite) is its own quartzite; Super White Dolomite is unchanged; and no alias
binds one stone's listing to the other. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
BACKBONE = REPO / "catalog_source" / "backbone_varieties.json"
ORIGIN_MAP = REPO / "catalog_source" / "origin_map.csv"

BRAZILIAN_WHITE_TYPES = ("Marble", "Onyx", "Quartzite")
# the other stone's names: never an alias of the one
SUPER_WHITE_NAMES = {"super white", "super white marble", "super white quartzite", "fjord white quartzite",
                     "white super", "white super es"}
BRAZILIAN_WHITE_NAMES = {"brazilian white", "brazilian white quartzite", "brazil arabescato",
                         "brazil arabescato marble", "yudu hui"}


def _base_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as h:
        return {(r["Type"], r["Name"]): r for r in csv.DictReader(h)}


def _aliases(text):
    return {a.strip().lower() for a in (text or "").split("|") if a.strip()}


def _posts():
    return {(p["stone_type"], p["variant"]): p for p in json.loads(BACKBONE.read_text(encoding="utf-8"))["posts"]}


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_brazilian_white_is_its_own_variety_in_every_base(base):
    rows = _base_rows(base)
    for stone_type in BRAZILIAN_WHITE_TYPES:
        row = rows.get((stone_type, "Brazilian White"))
        assert row is not None, f"no {stone_type} Brazilian White in {base}"
        assert row["Key"] == new_key(stone_type, "Brazilian White")
        assert not _aliases(row["Aliases"]) & SUPER_WHITE_NAMES
    assert ("Marble", "Super White") not in rows and ("Onyx", "Super White") not in rows
    quartzite = rows[("Quartzite", "Super White")]
    assert quartzite["Key"] == new_key("Quartzite", "Super White")
    assert _aliases(quartzite["Aliases"]) == {"super white quartzite", "fjord white quartzite"}
    assert rows[("Dolomite", "Super White")]["Key"] == new_key("Dolomite", "Super White")


def test_the_backbone_holds_the_two_stones_apart():
    posts = _posts()
    for stone_type in BRAZILIAN_WHITE_TYPES:
        post = posts.get((stone_type, "Brazilian White"))
        assert post is not None and post["key"] == new_key(stone_type, "Brazilian White")
        assert not {a.lower() for a in post["aliases"]} & SUPER_WHITE_NAMES
    assert ("Marble", "Super White") not in posts and ("Onyx", "Super White") not in posts
    assert not {a.lower() for a in posts[("Quartzite", "Super White")]["aliases"]} & BRAZILIAN_WHITE_NAMES


def test_each_identity_has_its_origin_row():
    with ORIGIN_MAP.open(encoding="utf-8-sig", newline="") as h:
        rows = {(r["variety"], r["stone_type"]) for r in csv.DictReader(h)}
    for identity in [("Brazilian White", t) for t in BRAZILIAN_WHITE_TYPES] + [("Super White", "Quartzite"),
                                                                                  ("Super White", "Dolomite")]:
        assert identity in rows, identity
    assert ("Super White", "Marble") not in rows and ("Super White", "Onyx") not in rows


@pytest.fixture(scope="module")
def stage():
    return match_variation.VariationStage.build(loaders.load_all())


def _bind(stage, src_site, name, stone_type):
    row = CanonicalRow(src_site=src_site, surrogate_key="x", format_value="Slab", variety_match_key=name,
                       raw_type=stone_type, raw_color="White")
    stage.resolve_row(row)
    return row.variation_key


def test_a_brazilian_white_listing_binds_brazilian_white(stage):
    # marenostone P/06/22 "Brazilian White Quartzite Slab" and polonine "FJORD WHITE QUARTZITE" (fixtures
    # data/marenostone/20260710_155335, data/polonine/20260619_214426) are two stones, each on its own quartzite
    assert _bind(stage, "marenostone", "Brazilian White Quartzite", "Quartzite") == new_key("Quartzite",
                                                                                          "Brazilian White")
    assert _bind(stage, "polonine", "Fjord White Quartzite", "Quartzite") == new_key("Quartzite", "Super White")
    assert _bind(stage, "polonine", "Super White", "Dolomite") == new_key("Dolomite", "Super White")


def test_a_super_white_marble_listing_never_binds_brazilian_white(stage):
    brazilian = _bind(stage, "marenostone", "Brazilian White", "Marble")
    assert brazilian == new_key("Marble", "Brazilian White")
    assert _bind(stage, "marenostone", "Super White", "Marble") != brazilian
