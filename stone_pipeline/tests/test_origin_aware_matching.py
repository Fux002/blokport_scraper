"""Origin as a first-class MATCH signal (never an identity field).

A trade name means different stones to different sellers ('Amazon Green Granite' is an alias of Amazon Blue,
Amazonia and Verde Ubatuba). The engine narrows same-type ties by which candidates DOCUMENT the product's
origin evidence, holds an unresolved known-name collision for review instead of letting fuzzy/phonetic pick
one by string or sound, and applies the operator's vendor-scoped alias ('for this vendor, this spelling is
that variety') at its override tier. Hermetic: hand-built index + stub reference, runs in CI.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import match_variation
from stone_pipeline.state.overrides import Overrides
from stone_pipeline.state.writeback import WriteBack


def _engine():
    idx = CandidateIndex()
    # three DISTINCT granites that all carry the same trade-name alias; only one documents Iran
    idx.add("v_blue", "Amazon Blue", surfaces=["Amazon Green Granite"], block_type="granite",
            block_colors={"blue", "green"}, origins={"BR"})
    idx.add("v_amazonia", "Amazonia", surfaces=["Amazon Green Granite"], block_type="granite",
            block_colors={"green"}, origins={"BR"})
    idx.add("v_golden", "Golden Lightning", surfaces=["Amazon Green Granite"], block_type="granite",
            block_colors={"green", "gold"}, origins={"BR", "CN", "IR", "US"})
    # a lone variety with documented origins, to prove a single candidate is never rejected by origin
    idx.add("v_star", "Star Black", surfaces=[], block_type="granite", origins={"IN", "ZA"})
    # one name, two real stones: the type-conflict case normalize leaves open for origin to settle
    idx.add("v_azul_onyx", "Azul White", surfaces=[], block_type="onyx", block_colors={"white"}, origins={"IR"})
    idx.add("v_azul_qtz", "Azul White", surfaces=[], block_type="quartzite", block_colors={"white"}, origins={"BR"})
    return VariationEngine(idx, auto_accept=92, review_floor=84)


# --- engine: origin narrows, never picks --------------------------------------------------------------

def test_origin_narrows_a_shared_alias_to_the_corroborated_variety():
    m = _engine().match("Amazon Green Granite", block_type="granite", block_origin="IR")
    assert m.cid == "v_golden" and m.method.endswith("_origin") and m.confidence >= 2


def test_uncorroborated_collision_is_held_for_review_with_the_owners_not_resolved_by_sound():
    # a Brazilian seller: all three document Brazil -> still a tie -> review listing them; no phonetic pick
    m = _engine().match("Amazon Green Granite", block_type="granite", block_origin="BR")
    assert m.cid is None and m.method.endswith("_collision") and not m.ambiguous
    assert {c[1] for c in m.candidates} == {"Amazon Blue", "Amazonia", "Golden Lightning"}


def test_no_origin_evidence_leaves_the_collision_for_review_too():
    m = _engine().match("Amazon Green Granite", block_type="granite")
    assert m.cid is None and m.method.endswith("_collision")


def test_a_reseller_country_documented_by_nobody_corroborates_nothing():
    # Turkey is documented by none of the three -> the set is unchanged (no evidence against any) -> review
    m = _engine().match("Amazon Green Granite", block_type="granite", block_origin="TR")
    assert m.cid is None and m.method.endswith("_collision")


def test_a_single_candidate_is_never_rejected_by_origin():
    # origin narrows ties only: a lone variety binds even when the evidence is not among its origins
    m = _engine().match("Star Black", block_type="granite", block_origin="IR")
    assert m.cid == "v_star" and m.method == "exact"


def test_a_fuzzy_tie_is_broken_by_origin_with_the_same_rule():
    idx = CandidateIndex()
    # two candidates one substitution away from the query at the same position (equal fuzzy score) whose
    # sound differs from the query's, so the phonetic tier stays out and the fuzzy tie is what is broken
    idx.add("a", "Kashmir Golda", surfaces=[], block_type="granite", origins={"BR"})
    idx.add("b", "Kashmir Golde", surfaces=[], block_type="granite", origins={"IN"})
    eng = VariationEngine(idx, auto_accept=90, review_floor=80)
    m = eng.match("Kashmir Goldx", block_type="granite", block_origin="IN")
    assert m.cid == "b" and m.method == "fuzzy_origin"


# --- stage: evidence resolution + vendor-scoped alias --------------------------------------------------

def _ref(scoped=None):
    return SimpleNamespace(
        overrides=Overrides(by_key={}),
        variants={"slab": SimpleNamespace(by_id={
            "v_blue": SimpleNamespace(key="slab_granite_amazon_blue_1"),
            "v_amazonia": SimpleNamespace(key="slab_granite_amazonia_2"),
            "v_golden": SimpleNamespace(key="slab_granite_golden_lightning_3"),
            "v_star": SimpleNamespace(key="slab_granite_star_black_4"),
            "v_azul_onyx": SimpleNamespace(key="slab_onyx_azul_white_5"),
            "v_azul_qtz": SimpleNamespace(key="slab_quartzite_azul_white_6")})},
        variety_seed_types={},
        scoped_aliases=scoped or {},
        country_codes={"iran": "IR", "brazil": "BR"},
        valid_iso_codes=frozenset({"IR", "BR", "IN", "TR"}),
        to_iso=lambda v: {"iran": "IR", "brazil": "BR", "ir": "IR", "br": "BR"}.get((v or "").strip().lower()),
    )


def _stage(scoped=None, primary_origin="IR"):
    return match_variation.VariationStage(ref=_ref(scoped), engines={"slab": _engine()}, writeback=WriteBack(),
                                          source_cfg=SimpleNamespace(primary_origin=primary_origin))


def _row(match_key, raw_origin=None, src="marenostone"):
    return CanonicalRow(src_site=src, surrogate_key="1", variety_match_key=match_key, raw_type="Granite",
                        raw_format="Slab", raw_origin=raw_origin)


def test_origin_evidence_prefers_the_scraped_field_over_the_vendor_declaration():
    ref = _ref()
    assert match_variation.origin_evidence(_row("x", raw_origin="Brazil"), SimpleNamespace(primary_origin="IR"), ref) == "BR"
    assert match_variation.origin_evidence(_row("x"), SimpleNamespace(primary_origin="Iran"), ref) == "IR"
    assert match_variation.origin_evidence(_row("x"), None, ref) == ""


def test_stage_binds_the_iranian_listing_to_the_variety_that_documents_iran():
    row = _row("Amazon Green Granite")
    _stage().resolve_row(row)
    assert row.variation_id == "v_golden" and row.variation_key == "slab_granite_golden_lightning_3"


def test_stage_holds_the_brazilian_listing_for_review_instead_of_a_phonetic_bind():
    row = _row("Amazon Green Granite", src="zucchi")
    _stage(primary_origin="BR").resolve_row(row)
    assert row.variation_id is None and row.variation_method.endswith("_collision")


def test_vendor_scoped_alias_binds_only_for_that_vendor():
    scoped = {("marenostone", "amazon green granite"): ("Amazonia", "")}
    row = _row("Amazon Green Granite", src="marenostone")
    _stage(scoped).resolve_row(row)
    assert row.variation_id == "v_amazonia" and row.variation_method == "override"
    other = _row("Amazon Green Granite", src="polonine")
    _stage(scoped, primary_origin="BR").resolve_row(other)
    assert other.variation_id is None                       # the scope does not leak to another vendor


def test_scoped_alias_whose_target_is_not_one_variety_is_ignored_loudly():
    scoped = {("marenostone", "amazon green granite"): ("Nobody", "")}
    row = _row("Amazon Green Granite")
    _stage(scoped).resolve_row(row)
    assert row.variation_id == "v_golden"                    # falls back to the normal origin narrowing


# --- decisions: storage + the origin card carries the vendor's spelling --------------------------------

def test_scoped_alias_store_roundtrip_and_pristine_clear(tmp_path, monkeypatch):
    from stone_pipeline.config import decisions_store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    assert decisions_store.scoped_aliases() == {}            # absent store -> empty, and NOT created
    decisions_store.set_scoped_alias("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite")
    assert decisions_store.scoped_aliases() == {("marenostone", "amazon green granite"): ("Golden Lightning", "Granite")}
    decisions_store.set_scoped_alias("marenostone", "Amazon Green Granite", "Amazonia")      # idempotent upsert
    assert decisions_store.scoped_aliases()[("marenostone", "amazon green granite")] == ("Amazonia", "")
    with pytest.raises(decisions_store.InvalidDecision):
        decisions_store.set_scoped_alias("", "x", "y")
    assert decisions_store.clear_scoped_aliases() == 1 and decisions_store.scoped_aliases() == {}


def test_origin_card_carries_the_scraped_spelling(tmp_path, monkeypatch):
    from stone_pipeline.config import decisions_store
    from stone_pipeline.core.schema import FlagCode, ReviewFlag
    from stone_pipeline.stages import decisions
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    row = _row("Amazon Green Granite")
    row.variation_name, row.type_name, row.origin_source = "Amazonia", "Granite", "origin_needs_confirmation"
    row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_needs_confirmation, raw_value="BR",
                            best_guess="IR", confidence=0, method="vendor_map_mismatch"))
    assert decisions.write_origin_confirm_file([row]) == 1
    card = decisions_store.list_pending("origin")[0]
    assert card["scraped"] == "Amazon Green Granite" and card["variety"] == "Amazonia"
    assert card["map_country"] == "BR" and card["vendor_origin"] == "IR"


# --- stage: a type left open by the name/tag conflict is settled by origin, else held --------------------

def test_type_open_listing_binds_the_variety_whose_origin_the_vendor_corroborates():
    # normalize left the type open ('Azul White Quartzite' tagged Onyx, both exist); the Iranian vendor's
    # evidence picks the onyx (documents IR) over the quartzite (BR), with no card.
    row = CanonicalRow(src_site="marenostone", surrogate_key="1", variety_match_key="Azul White Quartzite",
                       raw_type="", raw_format="Slab")
    _stage().resolve_row(row)
    assert row.variation_id == "v_azul_onyx" and row.variation_key == "slab_onyx_azul_white_5"
    assert row.variation_method.endswith("_origin")


def test_type_open_listing_without_origin_evidence_is_held_not_guessed():
    row = CanonicalRow(src_site="polonine", surrogate_key="1", variety_match_key="Azul White Quartzite",
                       raw_type="", raw_format="Slab")
    _stage(primary_origin="").resolve_row(row)
    assert row.variation_id is None
    assert row.tree_gaps and any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)
