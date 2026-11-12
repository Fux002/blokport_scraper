"""An alias never equals the name of another variety of the SAME type (Mas, 11-11): the rule on small cases, then
over the WHOLE committed vocabulary (both bases and the backbones). A failure lists every collision, each with its
variety and the variety it names. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.reference import alias_collisions as ac

REPO = Path(__file__).resolve().parents[2]
BASES = sorted((REPO / "from_medusa").glob("*/variants_export_base.csv"))
BACKBONES = [REPO / "catalog_source" / "backbone_varieties.json",
             REPO / "catalog_source" / "backbone_additions" / "varieties.json"]


def _v(key, stone_type, name, *aliases):
    return ac.Variety(key, stone_type, name, tuple(aliases))


def test_an_alias_naming_another_variety_collides():
    found = ac.find([_v("m1", "Marble", "Brazilian White", "White Super"), _v("m2", "Marble", "White Super")])
    assert [(c.alias, c.variety, c.collides_with) for c in found] == [
        ("White Super", ("Marble", "Brazilian White"), ("Marble", "White Super"))]
    with pytest.raises(ac.AliasCollision, match="'White Super' of Marble 'Brazilian White'"):
        ac.check([_v("m1", "Marble", "Brazilian White", "White Super"), _v("m2", "Marble", "White Super")])


def test_its_own_name_and_its_own_copies_are_not_collisions():
    # the same identity in two categories, and an alias that only repeats the name with its type word
    assert ac.find([_v("slab_q", "Quartzite", "Super White", "Super White Quartzite"),
                    _v("block_q", "Quartzite", "Super White", "Super White Quartzite")]) == []


def test_the_same_name_under_another_type_is_not_a_collision():
    # a granite 'Silver Grey' alias and a slate 'Silver Grey' variety are two stones matching keeps apart (Mas, 11-11)
    assert ac.find([_v("g1", "Granite", "Bianco Sardo", "Silver Grey"), _v("s1", "Slate", "Silver Grey")]) == []


def _committed():
    out = []
    for base in BASES:
        with base.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                out.append(_v(r["Key"], r["Type"], r["Name"], *[a for a in (r["Aliases"] or "").split("|") if a]))
    for path in BACKBONES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for p in (doc["posts"] if isinstance(doc, dict) else doc):
            if p.get("stone_type"):                       # an untyped addition is held, never a variety
                out.append(_v(p.get("key") or f"{path.name}:{p['variant']}", p["stone_type"], p["variant"],
                              *p.get("aliases", [])))
    return out


def test_no_committed_alias_names_another_variety():
    collisions = ac.find(_committed())
    assert not collisions, (f"{len(collisions)} committed alias(es) name another variety of the same type:\n"
                            + "\n".join(str(c) for c in collisions))


def test_the_build_fails_loud_on_a_new_same_type_collision(tmp_path):
    # the build-time gate: a variety file that would ship an alias naming another same-type variety stops the
    # build with every collision named, before anything is written
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.stages import emit_catalog
    base = tmp_path / "variants_export_base.csv"
    with base.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([emit_catalog._COLS,
                                 [new_key("Marble", "Brazilian White"), "Brazilian White", "Marble", "White Super"],
                                 [new_key("Marble", "White Super"), "White Super", "Marble", ""]])
    out = tmp_path / "1_variants_full.csv"
    with pytest.raises(ac.AliasCollision, match="'White Super' of Marble 'Brazilian White'"):
        emit_catalog.build(base, seed_only=True, out_path=out)
    assert not out.exists()
