"""SC-4 curate (PHASE-1-SPEC rev 8 section 6.2 "Curate"; SC-3-SC-4-PLAN.md section 2a): a variety is ONE record per
(type, name), so curate writes one row, one backbone post and one image item per category its products sit in,
never a copy per category. The Key is the one rule (phase1.variety_key.new_key) and its type is the Key's type slug:
(type, variant) stays one atomic identity, so one name under two types is two varieties. No em dashes."""
from __future__ import annotations

import json
import types

import pytest

from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.phase1 import keymap, variety_key
from stone_pipeline.reference import clean_committed_names as ccn
from stone_pipeline.reference.loaders import type_slug_from_key
from stone_pipeline.reference.loaders.attributes import load_attributes
from stone_pipeline.stages import curate, decisions
from stone_pipeline.stages.curate import existing as existing_index

GRANITE_ALPINE = variety_key.new_key("Granite", "Alpine")
MARBLE_ALPINE = variety_key.new_key("Marble", "Alpine")


@pytest.fixture
def ref(tmp_path, monkeypatch):
    """A curation world on the new Keys: an export of two same-named varieties of two types, no alias model,
    nothing retired. Returns the reference data curate reads (the attribute vocabulary)."""
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Aliases,status,Type,owner\n"
                      f"v1,{GRANITE_ALPINE},Alpine,Alpine White,active,Granite,scraper\n"
                      f"v2,{MARBLE_ALPINE},Alpine,,active,Marble,scraper\n", encoding="utf-8")
    monkeypatch.setattr(existing_index, "existing_varieties_file", lambda: export)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    monkeypatch.setattr(decisions, "load_retired", set)
    return types.SimpleNamespace(attributes=load_attributes())


def _gapped(name: str, stone_type: str, fmt: str, n: str) -> CanonicalRow:
    row = CanonicalRow(src_site="polonine", surrogate_key=n, variety_match_key=name, raw_type=stone_type,
                       format_value=fmt)
    row.add_gap(TreeGap(src_site="polonine", surrogate_key=n, raw_name=name, gap_kind=GapKind.missing_variation,
                        nearest_existing="Something", nearest_score=40.0))
    return row


def _minting(*spellings: str) -> Decisions:
    return Decisions.from_legacy({("", s): {"action": "mint"} for s in spellings})


def test_the_curate_key_is_the_one_rule_and_names_no_category():
    key = curate.gen_key("Dolomite Marble", "Calacatta Gold")
    assert key == variety_key.new_key("Dolomite Marble", "Calacatta Gold")
    assert type_slug_from_key(key) == "dolomite_marble"          # the type is the Key's type slug, whole
    for gone in ("BRANCHES", "active_branches", "core", "image_filename", "image_url", "load_all_existing"):
        assert not hasattr(curate, gone), gone


def test_a_new_variety_sold_as_slab_and_block_is_one_row_one_post_and_an_image_per_category(ref):
    rows = [_gapped("Novel Xyz", "Granite", "slab", "1"), _gapped("Novel Xyz", "Granite", "block", "2")]
    result = curate.build_curation(rows, ref, _minting("novel xyz"))
    key = variety_key.new_key("Granite", "Novel Xyz")
    assert result.new_variants == [{
        "Key": key, "Name": "Novel Xyz", "Type": "Granite", "Aliases": "",
        "_nearest_existing": "Something", "_nearest_score": 40.0, "_suggested_type": "Granite",
        "_example_url": "",
    }]
    [post] = result.backbone_new
    assert post["key"] == key and post["variant"] == "Novel Xyz" and post["stone_type"] == "Granite"
    assert post["categories"] == ["block", "slab"]
    assert not {"category", "image_file", "product_backed"} & set(post)
    assert result.images_to_generate == [
        {"key": key, "category": "block", "variant": "Novel Xyz", "status": "to_generate"},
        {"key": key, "category": "slab", "variant": "Novel Xyz", "status": "to_generate"},
    ]


def test_one_name_under_two_types_is_two_varieties_each_keyed_by_its_own_type(ref):
    rows = [_gapped("Novelo", "Granite", "slab", "1"), _gapped("Novelo", "Marble", "slab", "2")]
    result = curate.build_curation(rows, ref, _minting("novelo"))
    keys = sorted(r["Key"] for r in result.new_variants)
    assert keys == sorted([variety_key.new_key("Granite", "Novelo"), variety_key.new_key("Marble", "Novelo")])
    assert {type_slug_from_key(r["Key"]): r["Type"] for r in result.new_variants} == {
        "granite": "Granite", "marble": "Marble"}


def test_an_existing_variety_with_a_stored_mint_decision_is_never_minted_again(ref):
    result = curate.build_curation([_gapped("Alpine", "Granite", "block", "1")], ref, _minting("alpine"))
    assert result.new_variants == [] and result.backbone_new == [] and result.images_to_generate == []


def test_a_learned_alias_lands_on_the_one_row_of_the_matched_type(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="1", variety_match_key="Alpine Qx9", variation_id="v1",
                       variation_key=GRANITE_ALPINE, variation_name="Alpine", variation_method="fuzzy",
                       format_value="tile")
    result = curate.build_curation([row], ref, Decisions.empty())
    assert result.alias_additions == [{
        "Key": GRANITE_ALPINE, "Name": "Alpine", "Type": "Granite", "Aliases": "Alpine White|Alpine Qx9",
        "_added": "Alpine Qx9", "_status": "confirmed",
    }]


def test_a_mint_onto_a_retired_key_holds_for_un_retire_and_mints_nothing(ref, monkeypatch):
    monkeypatch.setattr(decisions, "load_retired", lambda: {variety_key.new_key("Granite", "Novel Xyz")})
    result = curate.build_curation([_gapped("Novel Xyz", "Granite", "slab", "1")], ref, _minting("novel xyz"))
    assert result.new_variants == []
    assert [p["kind"] for p in result.pending_confirm if p["variant"] == "Novel Xyz"] == ["retired"]


def test_the_minted_membership_reads_the_flat_post_list(monkeypatch):
    recorded = []
    monkeypatch.setattr(decisions.decisions_store, "record_minted_membership",
                        lambda m: recorded.extend(m) or len(m))
    post = {"variant": "Novel Xyz", "stone_type": "Granite", "color": ["Black"], "finishes": ["Polished"],
            "qualities": ["Standard"]}
    assert decisions.write_minted_membership([post, dict(post)]) == 3
    assert recorded == [("Novel Xyz", "Granite", "color", "Black"), ("Novel Xyz", "Granite", "finish", "Polished"),
                        ("Novel Xyz", "Granite", "quality", "Standard")]


def test_the_key_map_s_category_rank_is_the_pack_order():
    # SC-1's union key map ranks a variety's copies by the pack's category order: it must not move
    assert keymap.BRANCH_ORDER == tuple(c.name for c in CATEGORIES)


def test_the_committed_names_cleaner_refuses_a_clean_that_moves_a_key():
    before = {ccn.SLAB_BACKBONE: json.dumps({"posts": [{"variant": "Alpine", "stone_type": "Granite"}]})}
    after = {ccn.SLAB_BACKBONE: json.dumps({"posts": [{"variant": "Alpine Grey", "stone_type": "Granite"}]})}
    ccn._check_keys_stay(before, before)
    with pytest.raises(ccn.Refused, match="1 Keys would move"):
        ccn._check_keys_stay(before, after)
