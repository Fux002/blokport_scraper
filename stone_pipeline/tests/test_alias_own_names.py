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
# P2-10 seed rewrite (manager, 11-12): a post's variant is its base Name, so the coded forms the base folds in
# ('Rosal B') are the post's own other spellings; the base Name itself, or it plus a type word, is never an alias (C1:
# own_name, own_name_type_word; those values are on catalog_source/seed_alias_differences.csv).
POST_OWN_NAMES = [
    ("granite__red_wendeng_1c5966fd-56a8-5df3-b37e-b20634adc177", "Red Wendeng-z"),
    ("granite__white_fedea2ea-9c94-55c8-9526-95cd6fb69669", "White G"),
    ("limestone__hauteville_97a37bf1-a5e5-5e26-8164-128847130b9d", "Hauteville C"),
    ("limestone__karina_db425933-60aa-5c11-a839-562a73e6f59d", "Karina V"),
    ("limestone__rosal_10cbf934-0687-5e6b-a23b-50afe310f65f", "Rosal B"),
    ("limestone__sandy_fb75c39d-5e1e-5dee-8c73-79dd6a4b8f9f", "Sandy P"),
    ("marble__ariston_7adf471f-f9ac-5745-afa6-fefaf2f3e4da", "Ariston V"),
    ("tuff__armenia_27497efe-d75d-5b11-8f47-52746854f388", "Armenia L"),
]
# a code is the post's own spelling too, held in `codes` (C1: a code is never an alias)
POST_OWN_CODES = [
    ("granite__g562_b100fad8-299d-5da7-b176-fd0ad674ce27", "G562"),
    ("granite__g635_a7826fd2-391e-5463-a442-be35a2fcfc2a", "G635"),
]


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_each_base_row_keeps_its_own_other_spellings(base):
    with base.open(encoding="utf-8-sig", newline="") as h:
        aliases = {r["Key"]: set(filter(None, r["Aliases"].split("|"))) for r in csv.DictReader(h)}
    assert [(k, a) for k, a in BASE_OWN_NAMES if a not in aliases.get(k, set())] == []


def test_each_backbone_post_keeps_its_own_other_spellings():
    posts = {p["key"]: p for p in json.loads(BACKBONE.read_text(encoding="utf-8"))["posts"]}
    assert [(k, a) for k, a in POST_OWN_NAMES if a not in posts[k]["aliases"]] == []
    assert [(k, c) for k, c in POST_OWN_CODES if c not in posts[k].get("codes", [])] == []
