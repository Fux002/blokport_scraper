"""Calcite Blue and Casa Blue are the SAME stone (owner, 2026-11-12): 'Calcite Blue' is an alias of marble Casa Blue
again (SC #635's same-type cleanup removed it because a marble 'Calcite Blue' had been minted), and no committed
store holds a separate marble Calcite Blue variety, so nothing mints it. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
BACKBONES = [REPO / "catalog_source" / "backbone_varieties.json",
             REPO / "catalog_source" / "backbone_additions" / "varieties.json"]
CASA_BLUE_KEY = "marble__casa_blue_493ccf20-b050-5fec-b2f0-17a91fe55ff5"
CALCITE_BLUE_NAMES = {"Calcite Blue", "Calcite Blue Marble"}


def _posts(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["posts"] if isinstance(data, dict) else data


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_casa_blue_base_row_carries_calcite_blue(base):
    with base.open(encoding="utf-8-sig", newline="") as h:
        rows = {r["Key"]: r for r in csv.DictReader(h)}
    assert CALCITE_BLUE_NAMES <= set(rows[CASA_BLUE_KEY]["Aliases"].split("|"))
    assert [k for k, r in rows.items() if r["Type"] == "Marble" and r["Name"] in CALCITE_BLUE_NAMES] == []


def test_casa_blue_post_carries_calcite_blue():
    posts = {p["key"]: p for p in _posts(BACKBONES[0])}
    assert CALCITE_BLUE_NAMES <= set(posts[CASA_BLUE_KEY]["aliases"])


@pytest.mark.parametrize("backbone", BACKBONES, ids=lambda p: p.parent.name + "/" + p.name)
def test_no_separate_marble_calcite_blue_variety(backbone):
    assert [p["key"] for p in _posts(backbone)
            if p.get("stone_type") == "Marble" and p.get("variant") in CALCITE_BLUE_NAMES] == []
