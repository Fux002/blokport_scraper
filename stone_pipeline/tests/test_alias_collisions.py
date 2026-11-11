"""An alias never equals another variety's name (Mas, 11-11): the rule on small cases, then over the WHOLE committed
vocabulary of the candidate (both bases, the three backbones and their additions, old Keys). The type of a base row is
its Key's type slug, named through the committed attributes. A failure lists every collision, each with its variety
and the variety it names: that list is the report Mas rules on. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.core.text import slugify
from stone_pipeline.reference import alias_collisions as ac
from stone_pipeline.reference.loaders import load_attributes, type_slug_from_key

REPO = Path(__file__).resolve().parents[2]
BASES = sorted((REPO / "from_medusa").glob("*/variants_export_base.csv"))
CATALOG = REPO / "catalog_source"
BACKBONES = [CATALOG / f"backbone_{c}.json" for c in ("slabs", "blocks", "tiles")] + \
            [CATALOG / "backbone_additions" / f"{c}.json" for c in ("slab", "block", "tile")]


def _v(key, stone_type, name, *aliases):
    return ac.Variety(key, stone_type, name, tuple(aliases))


def test_an_alias_naming_another_variety_collides():
    found = ac.find([_v("m1", "Marble", "Brazilian White", "White Super"), _v("m2", "Marble", "White Super")])
    assert [(c.alias, c.variety, c.collides_with) for c in found] == [
        ("White Super", ("Marble", "Brazilian White"), ("Marble", "White Super"))]
    with pytest.raises(ac.AliasCollision, match="'White Super' of Marble 'Brazilian White'"):
        ac.check([_v("m1", "Marble", "Brazilian White", "White Super"), _v("m2", "Marble", "White Super")])


def test_its_own_name_and_its_own_copies_are_not_collisions():
    assert ac.find([_v("slab_q", "Quartzite", "Super White", "Super White Quartzite"),
                    _v("block_q", "Quartzite", "Super White", "Super White Quartzite")]) == []


def _committed():
    type_of = {slugify(t, "_"): t for t in load_attributes().canonical_names("type")}
    out = []
    for base in BASES:
        with base.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                slug = type_slug_from_key(r["Key"])
                out.append(_v(r["Key"], type_of.get(slug, slug), r["Name"],
                              *[a for a in (r["Aliases"] or "").split("|") if a]))
    for path in BACKBONES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for p in (doc["posts"] if isinstance(doc, dict) else doc):
            if p.get("stone_type"):                       # an untyped addition is held, never a variety
                out.append(_v(p.get("key") or f"{path.name}:{p['variant']}", p["stone_type"], p["variant"],
                              *p.get("aliases", [])))
    return out


def test_no_committed_alias_names_another_variety():
    collisions = ac.find(_committed())
    assert not collisions, (f"{len(collisions)} committed alias(es) name another variety:\n"
                            + "\n".join(str(c) for c in collisions))
