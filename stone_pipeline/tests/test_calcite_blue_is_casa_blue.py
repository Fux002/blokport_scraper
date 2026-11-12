"""Calcite Blue and Casa Blue are the SAME stone (owner, 2026-11-12), on the candidate's OLD branch-prefixed Keys:
'Calcite Blue' is an alias of marble Casa Blue in every branch again (SC #637's same-type cleanup removed it because
a marble 'Calcite Blue' had been minted), and no committed store holds a separate marble Calcite Blue, so nothing
mints it. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
CATALOG = REPO / "catalog_source"
BACKBONES = [CATALOG / f"backbone_{b}.json" for b in ("slabs", "blocks", "tiles")]
ADDITIONS = [CATALOG / "backbone_additions" / f"{b}.json" for b in ("slab", "block", "tile")]
CASA_BLUE_KEYS = ("slab_marble_casa_blue_3c8aae72-237b-4afa-8f9d-6aa951538f09",
                  "block_marble_casa_blue_0fc7a9b9-9e99-5bd7-a4fc-c64ce9687d41",
                  "tile_marble_casa_blue_8143c047-88e0-5660-bce9-5b457252c41f")
CALCITE_BLUE_NAMES = {"Calcite Blue", "Calcite Blue Marble"}


def _posts(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["posts"] if isinstance(data, dict) else data


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_casa_blue_base_rows_carry_calcite_blue(base):
    with base.open(encoding="utf-8-sig", newline="") as h:
        rows = {r["Key"]: r for r in csv.DictReader(h)}
    assert [k for k in CASA_BLUE_KEYS if not CALCITE_BLUE_NAMES <= set(rows[k]["Aliases"].split("|"))] == []
    assert [k for k, r in rows.items() if r["Name"] in CALCITE_BLUE_NAMES and "_marble_" in k] == []


@pytest.mark.parametrize("backbone", BACKBONES, ids=lambda p: p.name)
def test_casa_blue_posts_carry_calcite_blue(backbone):
    casa = [p for p in _posts(backbone) if p.get("variant") == "Casa Blue" and p.get("stone_type") == "Marble"]
    assert len(casa) == 1 and CALCITE_BLUE_NAMES <= set(casa[0]["aliases"])


@pytest.mark.parametrize("path", BACKBONES + ADDITIONS, ids=lambda p: p.parent.name + "/" + p.name)
def test_no_separate_marble_calcite_blue_variety(path):
    assert [p["key"] for p in _posts(path)
            if p.get("stone_type") == "Marble" and p.get("variant") in CALCITE_BLUE_NAMES] == []
