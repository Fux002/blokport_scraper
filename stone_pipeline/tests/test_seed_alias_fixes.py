"""The seed's source fixes (Mas, 13 Nov; the manager's car 3b): catalog_source/seed_alias_fixes.csv (Key, Value, Fix,
Reason) names each seed value that is a sentence fragment, a concatenation or mojibake, and what it becomes: the
replacement terms (`|`-joined) or nothing (dropped). The seed rewrite applies it to EVERY seed value (the post's own,
each base's, a base-only Key's) BEFORE C1's judgement, so no base or live addition brings a fixed value back, and lists
each fix in the owner's difference list as `fixed` with its reason. A fix that names no seed value refuses (a stale
fix fails loud). The type-word rule of a split is stated in its Reason: each split name ends in its type word.

Red form: names the fix adds are read with a default, so each case fails on its assert. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.aliases import seed_rewrite
from stone_pipeline.core.text import match_key

REPO = Path(__file__).resolve().parents[2]
FIXES = getattr(seed_rewrite, "FIXES", Path("catalog_source/seed_alias_fixes.csv"))
FIXED = getattr(seed_rewrite, "FIXED", "fixed")
NERO_PICASSO = "marble__nero_picasso_67b0c554-9dac-58a1-afd7-e795c5bbbe66"
SIENA_RIVER = "granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f"
SIENA_WHITE = "granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96"
CIPOLLINO = "marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a"
BLUE_COAST = "marble__blue_coast_92272076-a92a-5721-80a6-7daca3f0665d"
CREMA_DOLOMIT = "marble__crema_dolomit_2b4a0981-bc6b-5faa-b562-c75ca7123858"


def _fixes() -> list[dict]:
    with (REPO / FIXES).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _posts() -> dict[str, dict]:
    doc = json.loads((REPO / seed_rewrite.BACKBONE).read_text(encoding="utf-8"))
    return {p["key"]: p for p in doc["posts"] if p.get("key")}


def _differences(cls: str) -> list[dict]:
    with (REPO / seed_rewrite.DIFFERENCES).open(encoding="utf-8", newline="") as handle:
        return [r for r in csv.DictReader(handle) if r["Class"] == cls]


# --- the committed seed ----------------------------------------------------------------------------------------------
def test_the_fixed_posts_carry_the_real_terms():
    posts = _posts()
    assert {"Kırşehir Nero Picasso", "Kırşehir Nero Picasso Marble"} <= set(
        posts[NERO_PICASSO]["aliases"])
    assert {"Tropical Siena Bordeaux Granite", "White River Granite", "White River"} <= set(
        posts[SIENA_RIVER]["aliases"])
    assert {"Cotton White Granite", "White Siena Granite", "Cotton White", "White Siena"} <= set(
        posts[SIENA_WHITE]["aliases"])
    assert {"China Cipollino", "China Cipollino Marble"} <= set(posts[CIPOLLINO]["aliases"])


def test_no_post_holds_a_fixed_value():
    keys = {key: {match_key(v) for v in [*p["aliases"], *p["codes"]]} for key, p in _posts().items()}
    held = [(f["Key"], f["Value"]) for f in _fixes() if match_key(f["Value"]) in keys.get(f["Key"], set())]
    assert held == []


def test_every_fix_is_listed_once_for_the_owner_with_its_reason():
    listed = {(r["Key"], match_key(r["Value"])): r["Reason"] for r in _differences(FIXED)}
    fixes = {(f["Key"], match_key(f["Value"])): f for f in _fixes()}
    assert set(listed) == set(fixes)
    assert all(fixes[k]["Reason"] in listed[k] for k in fixes)
    assert "UNDETERMINABLE" in listed[(BLUE_COAST, match_key("C Te D"))]


def test_a_former_type_name_is_no_variety_s_alias():
    # the manager, 13 Nov: "Dolomite Marble" is the type #293 collapsed into Dolomite, not a name of one stone; as an
    # alias it would send every "Dolomite Marble" search to one marble variety
    assert "Dolomite Marble" not in _posts()[CREMA_DOLOMIT]["aliases"]
    listed = {(r["Key"], r["Value"]): r["Reason"] for r in _differences(FIXED)}
    assert "former type name" in listed.get((CREMA_DOLOMIT, "Dolomite Marble"), "")


def test_every_split_reason_states_the_type_word_rule():
    splits = [f for f in _fixes() if "|" in f["Fix"]]
    assert splits and all("type word" in f["Reason"] for f in splits)


# --- the rewrite's rule, on a tiny seed ------------------------------------------------------------------------------
_HEADER = "Key,Name,Type,Aliases\n"
_K1, _K2 = "granite__alpha_1", "granite__beta_2"


def _seed(root: Path, fixes: str, base_aliases: str = "Alpha Old|In China Market|Kept One") -> Path:
    for path in seed_rewrite.COMMITTED_BASES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"{_HEADER}{_K1},Alpha,Granite,{base_aliases}\n{_K2},Beta,Granite,In Russian\n",
                                 encoding="utf-8")
    (root / seed_rewrite.LIVE_ADDITIONS).parent.mkdir(parents=True, exist_ok=True)
    (root / seed_rewrite.LIVE_ADDITIONS).write_text(f"{_HEADER}{_K1},Alpha,Granite,alpha old\n", encoding="utf-8")
    post = {"key": _K1, "variant": "Alpha", "stone_type": "Granite", "aliases": ["Alpha Old", "Kept One"],
            "codes": [], "merged_from": []}
    (root / seed_rewrite.BACKBONE).write_text(json.dumps({"posts": [post]}), encoding="utf-8")
    (root / FIXES).write_text("Key,Value,Fix,Reason\n" + fixes, encoding="utf-8")
    return root


_TINY = (f"{_K1},Alpha Old,Alpha Gold|Alpha Granite Two,two names (type word)\n"
         f"{_K1},In China Market,,a framing fragment\n{_K2},In Russian,,a language label\n")


def _render(root: Path) -> tuple[dict, list[dict]]:
    out = seed_rewrite.render(root)
    posts = {p["key"]: p for p in json.loads(out[seed_rewrite.BACKBONE])["posts"]}
    rows = list(csv.DictReader(out[seed_rewrite.DIFFERENCES].decode("utf-8").splitlines()))
    return posts, rows


def test_a_fix_replaces_its_value_everywhere_before_the_judgement(tmp_path):
    posts, rows = _render(_seed(tmp_path, _TINY))
    # the post's own value, the bases' and the live addition's spelling of it are ONE fix; its terms join in place
    assert posts[_K1]["aliases"] == ["Alpha Gold", "Alpha Granite Two", "Kept One"]
    fixed = sorted((r["Key"], r["Value"]) for r in rows if r["Class"] == FIXED)
    assert fixed == [(_K1, "Alpha Old"), (_K1, "In China Market"), (_K2, "In Russian")]
    # a base-only Key's fixed value is listed `fixed`, never `no_post` (the empty no_post row names the Key)
    assert [(r["Key"], r["Value"]) for r in rows if r["Class"] == seed_rewrite.NO_POST] == [(_K2, "")]


def test_a_second_render_changes_nothing(tmp_path):
    root = _seed(tmp_path, _TINY)
    first = seed_rewrite.render(root)
    for path, data in first.items():
        (root / path).write_bytes(data)
    assert seed_rewrite.render(root) == first


def test_a_fix_that_names_no_seed_value_refuses(tmp_path):
    stale = getattr(seed_rewrite, "StaleFix", None)
    assert stale is not None, "seed_rewrite has no StaleFix"
    with pytest.raises(stale, match="Gone Value"):
        seed_rewrite.render(_seed(tmp_path, _TINY + f"{_K1},Gone Value,,no longer in the seed\n"))
