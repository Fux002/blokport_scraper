"""A variety keeps its OWN other spellings (Scraper M2 on #635): the same-type alias cleanup must never drop a name that
another row or post of the SAME variety (one Key) spells it by, e.g. 'G562 Maple Red' and its backbone post 'G562'.
These pairs were dropped by the first cleanup and are restored. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
BACKBONE = REPO / "catalog_source" / "backbone_varieties.json"

BASE_OWN_NAMES = [
    ("granite__red_wendeng_1c5966fd-56a8-5df3-b37e-b20634adc177", "Red Wendeng-z"),
    ("granite__red_wendeng_1c5966fd-56a8-5df3-b37e-b20634adc177", "Red Wendeng-z Granite"),
    ("granite__white_fedea2ea-9c94-55c8-9526-95cd6fb69669", "White G"),
    ("limestone__hauteville_97a37bf1-a5e5-5e26-8164-128847130b9d", "Hauteville C"),
    ("limestone__karina_db425933-60aa-5c11-a839-562a73e6f59d", "Karina V"),
    ("limestone__karina_db425933-60aa-5c11-a839-562a73e6f59d", "Karina V Limestone"),
    ("limestone__rosal_10cbf934-0687-5e6b-a23b-50afe310f65f", "Rosal B"),
    ("limestone__sandy_fb75c39d-5e1e-5dee-8c73-79dd6a4b8f9f", "Sandy P"),
    ("marble__ariston_7adf471f-f9ac-5745-afa6-fefaf2f3e4da", "Ariston V"),
    ("tuff__armenia_27497efe-d75d-5b11-8f47-52746854f388", "Armenia L"),
]
POST_OWN_NAMES = [
    ("granite__g562_b100fad8-299d-5da7-b176-fd0ad674ce27", "G562"),
    ("granite__g562_b100fad8-299d-5da7-b176-fd0ad674ce27", "G562 Granite"),
    ("granite__g635_a7826fd2-391e-5463-a442-be35a2fcfc2a", "G635"),
    ("granite__g635_a7826fd2-391e-5463-a442-be35a2fcfc2a", "G635 Granite"),
    ("limestone__hauteville_97a37bf1-a5e5-5e26-8164-128847130b9d", "Hauteville"),
    ("limestone__rosal_10cbf934-0687-5e6b-a23b-50afe310f65f", "Rosal"),
    ("marble__ariston_7adf471f-f9ac-5745-afa6-fefaf2f3e4da", "Ariston"),
    ("marble__connemara_a5976235-7be6-5c04-a8ab-86ecf49dad10", "Connemara"),
    ("marble__silver_wave_811329f3-9bdf-5687-ac21-ac9f88602017", "Silver Wave"),
    ("marble__silver_wave_811329f3-9bdf-5687-ac21-ac9f88602017", "Silver Wave Marble"),
]


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_each_base_row_keeps_its_own_other_spellings(base):
    with base.open(encoding="utf-8-sig", newline="") as h:
        aliases = {r["Key"]: set(filter(None, r["Aliases"].split("|"))) for r in csv.DictReader(h)}
    assert [(k, a) for k, a in BASE_OWN_NAMES if a not in aliases.get(k, set())] == []


def test_each_backbone_post_keeps_its_own_other_spellings():
    aliases = {p["key"]: set(p["aliases"]) for p in json.loads(BACKBONE.read_text(encoding="utf-8"))["posts"]}
    assert [(k, a) for k, a in POST_OWN_NAMES if a not in aliases.get(k, set())] == []
