"""P2-10: the committed seed rewritten ONCE, so the backbone is the whole seed store (stone_pipeline.aliases.seed_rewrite).
Reds (a), (b) and (d) of the manager's brief (11-12); (c), the seed fixed point and seed.verify, stays pinned by
test_cold_start_idempotence and test_committed_names_fixed_point. Exclusions are only the NAMED lists in
catalog_source/seed_alias_differences.csv: `removed` (exactly the shared C1 judgement, cleanup.seed_refusal),
`no_post` (the base-only Keys, the manager's ruling) and `fixed` (the source fixes table, test_seed_alias_fixes.py).
No em dashes."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stone_pipeline.core.alias_problem import code_matcher
from stone_pipeline.core.text import match_key

REPO = Path(__file__).resolve().parents[2]
BACKBONE = REPO / "catalog_source" / "backbone_varieties.json"
DIFFERENCES = REPO / "catalog_source" / "seed_alias_differences.csv"
# the committed bases, then what dev's LIVE base adds to them (reviewer 2's L2 on C3, the manager's requirement 11-12)
LIVE_ADDITIONS = REPO / "catalog_source" / "seed_live_additions_development.csv"
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")] + \
    [LIVE_ADDITIONS]
IS_CODE = code_matcher(r"[Gg]\d+[A-Za-z]?")
# Keys with no backbone post (SC-3 rev 2.3), measured 11-12: 67 in both committed bases, plus 46 dev minted since
NO_POST_KEYS = 113


def _posts() -> dict[str, dict]:
    return {p["key"]: p for p in json.loads(BACKBONE.read_text(encoding="utf-8"))["posts"] if p.get("key")}


def _differences(cls: str) -> list[dict]:
    if not DIFFERENCES.exists():
        return []
    with DIFFERENCES.open(encoding="utf-8", newline="") as handle:
        return [r for r in csv.DictReader(handle) if r["Class"] == cls]


def _base_rows() -> list[tuple[str, dict]]:
    out = []
    for base in BASES:
        with base.open(encoding="utf-8-sig", newline="") as handle:
            out += [(base.name, r) for r in csv.DictReader(handle)]
    return out


# --- dev's live base: only what it adds is committed, pinned by its S3 VersionId ------------------------------------
_COMMITTED = "Key,Name,Type,Aliases\nk1,Alpha,Granite,Old|Kept\nk3,Gamma,Marble,\n"
_LIVE = "Key,Name,Type,Aliases\nk1,Alpha,Granite,old|New One\nk2,Beta,Granite,B One|B Two\nk3,Gamma,Marble,\n"


def test_the_live_additions_are_only_what_the_live_base_adds():
    from stone_pipeline.aliases import seed_rewrite
    from stone_pipeline.reference.medusa_files import read_variants_export
    committed = read_variants_export(_COMMITTED, require_status=False, name="committed")
    assert seed_rewrite.live_additions(_LIVE, committed) == (
        "Key,Name,Type,Aliases\nk1,Alpha,Granite,New One\nk2,Beta,Granite,B One|B Two\n")


def test_a_live_row_that_renames_a_committed_key_refuses():
    from stone_pipeline.aliases import seed_rewrite
    from stone_pipeline.reference.medusa_files import read_variants_export
    committed = read_variants_export(_COMMITTED, require_status=False, name="committed")
    with pytest.raises(seed_rewrite.BasesDisagree, match="k1"):
        seed_rewrite.live_additions("Key,Name,Type,Aliases\nk1,Alfa,Granite,\n", committed)


# --- alias VALUES key by alias_key (the manager, 13 Nov; SC #717's rule), names stay on match_key -------------------
_SCRIPTS = "Wratza 维纳多|巴比伦金|白玫瑰"


def test_a_live_addition_keeps_a_non_latin_alias_distinct_from_its_latin_part():
    # match_key reads a mixed value as its Latin part ("wratza") and every pure-CJK value as "": both would vanish
    from stone_pipeline.aliases import seed_rewrite
    from stone_pipeline.reference.medusa_files import read_variants_export
    committed = read_variants_export("Key,Name,Type,Aliases\nk1,Alpha,Granite,Wratza\n", require_status=False,
                                     name="committed")
    live = f"Key,Name,Type,Aliases\nk1,Alpha,Granite,Wratza|{_SCRIPTS}\n"
    assert seed_rewrite.live_additions(live, committed) == f"Key,Name,Type,Aliases\nk1,Alpha,Granite,{_SCRIPTS}\n"


def test_the_rewrite_never_merges_a_non_latin_alias_into_another(tmp_path):
    from stone_pipeline.aliases import seed_rewrite
    for path in seed_rewrite.COMMITTED_BASES:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("Key,Name,Type,Aliases\ngranite__alpha_1,Alpha,Granite,Wratza\n", encoding="utf-8")
    (tmp_path / seed_rewrite.LIVE_ADDITIONS).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / seed_rewrite.LIVE_ADDITIONS).write_text(
        f"Key,Name,Type,Aliases\ngranite__alpha_1,Alpha,Granite,{_SCRIPTS}\n", encoding="utf-8")
    post = {"key": "granite__alpha_1", "variant": "Alpha", "stone_type": "Granite", "aliases": ["Wratza"],
            "codes": [], "merged_from": []}
    (tmp_path / seed_rewrite.BACKBONE).write_text(json.dumps({"posts": [post]}), encoding="utf-8")
    # the source fixes table is a required input (an absent one fails loud): none here, so the header only
    (tmp_path / seed_rewrite.FIXES).write_text(",".join(seed_rewrite.FIX_COLUMNS) + "\n", encoding="utf-8")
    doc = json.loads(seed_rewrite.render(tmp_path)[seed_rewrite.BACKBONE])
    # each value is its own alias: none collapsed to "" or merged into "Wratza" (joined in alias_key order)
    assert doc["posts"][0]["aliases"] == ["Wratza", *_SCRIPTS.split("|")]


def test_the_live_snapshot_is_pinned_and_committed():
    from stone_pipeline.aliases import seed_rewrite
    assert seed_rewrite.LIVE_BASE_VERSION_ID and len(seed_rewrite.LIVE_BASE_SHA256) == 64
    assert LIVE_ADDITIONS.read_text(encoding="utf-8").startswith("Key,Name,Type,Aliases\n")


# --- (a) every committed base alias is on its Key's post, or on a named list ---------------------------------------
def test_every_committed_base_alias_is_on_its_key_s_post_or_named():
    posts = {k: {match_key(v) for v in [*p.get("aliases", []), *p.get("codes", [])]} for k, p in _posts().items()}
    named = {(r["Key"], match_key(r["Value"])) for cls in ("removed", "fixed") for r in _differences(cls)}
    no_post = {r["Key"] for r in _differences("no_post")}
    missing = [f"{env} {r['Key']} {a!r}" for env, r in _base_rows() for a in (r["Aliases"] or "").split("|")
               if a and match_key(a) not in posts.get(r["Key"], set())
               and (r["Key"], match_key(a)) not in named and r["Key"] not in no_post]
    assert not missing, f"{len(missing)} base alias(es) on no post and no named list:\n" + "\n".join(missing[:50])


def test_the_no_post_list_names_exactly_the_base_keys_without_a_post():
    posts = _posts()
    base_only = {r["Key"] for _, r in _base_rows() if r["Key"] not in posts}
    assert len(base_only) == NO_POST_KEYS
    assert {r["Key"] for r in _differences("no_post")} == base_only


def test_the_removed_list_is_exactly_the_shared_c1_judgement():
    """Every listed value is refused by cleanup.seed_refusal in its Key's context (C2's build_contexts over the
    committed base varieties, the same judgement C5 classes `removed` by), and every base value it refuses is
    listed."""
    from stone_pipeline.aliases.cleanup import seed_refusal
    from stone_pipeline.aliases.context import build_contexts
    # the shared seed is judged against the COMMITTED names only, never one env's live mints (manager, 11-12)
    base = sorted({(r["Key"], r["Type"], r["Name"]) for source, r in _base_rows() if source != LIVE_ADDITIONS.name})
    contexts = build_contexts(base)
    live = {r["Key"]: (r["Type"], r["Name"]) for source, r in _base_rows() if source == LIVE_ADDITIONS.name}
    posts = _posts()

    def refusal(key: str, value: str):
        if key not in contexts:                      # a live-only Key or a post with no base row: its own name
            type_, name = live.get(key) or (posts[key]["stone_type"], posts[key]["variant"])
            contexts[key] = build_contexts([*base, (key, type_, name)])[key]
        return seed_refusal(value, contexts[key], IS_CODE)

    listed = _differences("removed")
    assert listed, "no removed list"
    wrong = [(r["Key"], r["Value"], r["Reason"]) for r in listed if refusal(r["Key"], r["Value"]) != r["Reason"]]
    assert not wrong, wrong[:20]
    # a source-fixed value is replaced before the judgement, so it is never judged (listed `fixed` instead)
    keys = {(r["Key"], match_key(r["Value"])) for r in [*listed, *_differences("fixed")]}
    unlisted = [(r["Key"], a) for _, r in _base_rows() for a in (r["Aliases"] or "").split("|")
                if a and refusal(r["Key"], a) and (r["Key"], match_key(a)) not in keys]
    assert not unlisted, unlisted[:20]


def test_named_moves():
    posts = _posts()
    assert {"Blue King A", "Blue King B"} <= set(posts["granite__blue_king_291b4daa-ef0b-52e9-8e47-807d49122a2e"]["aliases"])
    assert "Crema S" in posts["cantera__crema_5ff023ed-36bb-5f38-8c01-b7606cb3e408"]["aliases"]        # a coded name form
    assert {"G684", "G3518"} <= set(posts["basalt__china_black_6c9e08fc-b275-548f-86f7-a305be3d2af1"]["codes"])
    removed = {(r["Key"], r["Value"]): r["Reason"] for r in _differences("removed")}
    assert removed[("alabaster__angel_pink_dabe3591-f378-5a57-8d81-62d35d35ec60", "Angel Pink Alabaster")] == \
        "own_name_type_word"
    assert removed[("marble__white_super_0caae4a1-7d46-5dac-b34c-028b9616bac6", "White Super Es")] == "same_type_name"


def test_a_name_only_dev_minted_never_refuses_a_committed_alias():
    """Kept (manager, 11-12): 'Amazon Green' names a granite only dev's live base holds, so it stays on the posts it
    sits on; SC #638's gate enforces that collision per env at emit."""
    posts = _posts()
    for key in ("granite__verde_ubatuba_972a36ef-6ee7-5c61-8f5d-a784f1f9fe9d",
                "granite__amazon_blue_a80acbf2-a6e2-5596-b25a-4a1b24f6f75f",
                "granite__amazonia_34fba239-44f2-53cd-a95f-c94d82c47be4"):
        assert "Amazon Green" in posts[key]["aliases"], key


RENAMED_POSTS = 13                    # the manager's ruling (11-12): coded variants take the base Name


def test_a_coded_post_variant_takes_its_base_name_and_keeps_the_coded_form_as_an_alias():
    posts = _posts()
    names = {r["Key"]: r["Name"] for _, r in _base_rows()}
    renamed = _differences("renamed")
    assert len(renamed) == RENAMED_POSTS
    # the coded form stays through the base's alias, in the base's spelling ('Red Wendeng-z' for 'Red Wendeng-Z')
    assert [(r["Key"], r["Value"]) for r in renamed if posts[r["Key"]]["variant"] != names[r["Key"]]
            or match_key(r["Value"]) not in {match_key(a) for a in posts[r["Key"]]["aliases"]}] == []
    rosal = posts["limestone__rosal_10cbf934-0687-5e6b-a23b-50afe310f65f"]
    assert rosal["variant"] == "Rosal" and "Rosal B" in rosal["aliases"]


# --- (b) a code is never an alias, and every post states its codes -------------------------------------------------
def test_no_committed_post_alias_is_a_code():
    coded = [f"{k} {a!r}" for k, p in _posts().items() for a in p.get("aliases", []) if IS_CODE(a)]
    assert not coded, f"{len(coded)} post alias(es) are codes, they belong in `codes`:\n" + "\n".join(coded[:50])


def test_every_post_states_its_codes_and_each_is_a_code():
    posts = _posts().values()
    assert all(isinstance(p.get("codes"), list) for p in posts)
    assert [c for p in posts for c in p["codes"] if not IS_CODE(c)] == []


# --- (d) deterministic and idempotent --------------------------------------------------------------------------------
def test_a_second_run_changes_nothing():
    from stone_pipeline.aliases import seed_rewrite
    rendered = seed_rewrite.render()
    assert {p: rendered[p] == (REPO / p).read_bytes() for p in seed_rewrite.WRITES} == \
        {p: True for p in seed_rewrite.WRITES}


_DIGESTS = ("import hashlib; from stone_pipeline.aliases import seed_rewrite as s; "
            "print(*(hashlib.sha256(b).hexdigest() for _, b in sorted(s.render().items())))")


@pytest.mark.parametrize("hash_seed", ["0", "4242"])
def test_the_output_is_byte_identical_under_any_hash_seed(hash_seed):
    from stone_pipeline.aliases import seed_rewrite
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, "-c", _DIGESTS], cwd=REPO, env=env, capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == [hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in sorted(seed_rewrite.WRITES)]
