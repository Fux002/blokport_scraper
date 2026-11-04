"""Seed alias ownership: a spelling of one stone is an alias of that stone only.

'Pietra Gray' and 'Pietra Grey' are two spellings of one marble; the backbones and bases carried 'Pietra Gray Marble'
and 'Pietra Gray' on marble African Black, so marenostone's Pietra Gray tiles and slab bound to the wrong stone (owner
approval of the move, 2026-11-04)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPELLINGS = {"Pietra Gray Marble", "Pietra Gray"}
OWNER, TYPE = "Pietra Grey", "Marble"


def _assert_owned_by_pietra_grey(records):
    """`records` is (name, aliases) for every MARBLE record of one file: the owner exists, holds both spellings, and
    no other marble holds either."""
    owners = [aliases for name, aliases in records if name == OWNER]
    assert owners, "marble Pietra Grey is missing"
    for aliases in owners:
        assert SPELLINGS <= aliases
    holders = {name for name, aliases in records if SPELLINGS & aliases}
    assert holders == {OWNER}, holders


@pytest.mark.parametrize("path", ["catalog_source/backbone_slabs.json", "catalog_source/backbone_blocks.json",
                                  "catalog_source/backbone_tiles.json"])
def test_backbone_pietra_gray_spellings_belong_to_pietra_grey(path):
    posts = json.loads((ROOT / path).read_text(encoding="utf-8"))["posts"]
    _assert_owned_by_pietra_grey([(p["variant"], set(p["aliases"])) for p in posts if p["stone_type"] == TYPE])


@pytest.mark.parametrize("path", ["from_medusa/development/variants_export_base.csv",
                                  "from_medusa/production/variants_export_base.csv"])
def test_base_pietra_gray_spellings_belong_to_pietra_grey(path):
    with open(ROOT / path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["Key"].split("_")[1] == TYPE.lower()]
    _assert_owned_by_pietra_grey([(r["Name"], set(filter(None, r["Aliases"].split("|")))) for r in rows])
