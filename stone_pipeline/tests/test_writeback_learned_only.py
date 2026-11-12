"""The alias write-back keeps only learned GUESSES (owner, 2026-11-05).

A bind the seed already decides (exact, exact narrowed by name / type / colour / origin, projection, override)
is re-derived on every run, so saving it adds only stickiness: the saved pick outlived a later seed
correction. Prod: four marenostone 'Volakas Marble' slabs bound to Spider Red by origin (exact_origin) and the
pick was saved, so moving the seed alias to Volocas alone would have left them colliding. Since P2-10 C2 a learned
guess is a proposal, and the one-time migration of the old file drops every non-learned pick. Hermetic: a
hand-built index, runs in CI.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import match_variation
from stone_pipeline.state import writeback as wb
from stone_pipeline.tests.test_origin_aware_matching import _stage


@pytest.mark.parametrize("method, learned", [
    # every method the matcher binds with (engine.py tiers + the stage's wrappers), real strings from the
    # prod and dev files of 2026-11-05
    ("phonetic", True), ("fuzzy", True), ("fuzzy_origin", True),
    ("clean_variety_fuzzy", True), ("clean_variety_phonetic", True), ("operator_type_fuzzy", True),
    ("exact", False), ("exact_name", False), ("exact_blocked", False), ("exact_origin", False),
    ("exact_name_blocked", False), ("exact_name_origin", False), ("exact_blocked_origin", False),
    ("clean_variety_exact", False), ("clean_variety_exact_name_blocked", False), ("operator_type_exact", False),
    ("projection_compact", False), ("projection_tokenset_blocked_origin", False), ("projection_deprefixed", False),
    ("override", False), ("", False),
])
def test_only_phonetic_and_fuzzy_are_learned(method, learned):
    assert wb.is_learned(method) is learned


def test_a_bind_narrowed_by_origin_is_not_saved():
    stage = _stage()
    row = CanonicalRow(src_site="marenostone", surrogate_key="1", variety_match_key="Amazon Green Granite",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id == "v_golden" and row.variation_method.endswith("_origin")
    assert stage.writeback.aliases == []


def test_a_fuzzy_bind_is_still_saved():
    idx = CandidateIndex()
    idx.add("a", "Kashmir Golda", surfaces=[], block_type="granite", origins={"BR"})
    stage = match_variation.VariationStage(
        ref=SimpleNamespace(**{**vars(_stage().ref), "variants": {"slab": SimpleNamespace(
            by_id={"a": SimpleNamespace(key="granite__kashmir_golda_1", archived=False)})}}),
        engines={"slab": VariationEngine(idx, auto_accept=90, review_floor=80)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Goldx",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id == "a" and row.variation_method == "fuzzy"
    [(key, alias, method, source, score)] = stage.writeback.aliases
    assert (key, alias, method, source) == ("granite__kashmir_golda_1", "Kashmir Goldx", "fuzzy", "zucchi")
    assert 80 <= score < 100                                              # the fuzzy tier's own score


def _volakas_index():
    idx = CandidateIndex()
    idx.add("spider_red", "Spider Red", surfaces=["Red Spider"], block_type="marble", origins={"CN", "GR", "IR", "TR"})
    idx.add("volocas", "Volocas", surfaces=["Persian Volakas", "Volakas Marble"], block_type="marble",
            origins={"IR"})
    idx.add("jazz_white", "Jazz White", surfaces=["Volakas Marble"], block_type="marble", origins={"GR"})
    return idx


def test_an_old_saved_origin_pick_is_never_migrated_into_a_learned_alias():
    from stone_pipeline.aliases import migrate_learned
    from stone_pipeline.aliases.context import build_contexts
    ledger = [{"key": "marble__spider_red_1", "type": "Marble", "name": "Spider Red", "medusa_id": "spider_red",
               "in_full": 1, "state": "synced"}]
    active, dropped = migrate_learned.plan([("spider_red", "Volakas Marble", "exact_origin")], ledger,
                                           build_contexts([("marble__spider_red_1", "Marble", "Spider Red")]),
                                           lambda value: False)
    assert active == [] and [d["reason"] for d in dropped] == ["not_learned"]
    # why: the saved pick, had it been applied, makes Spider Red a second Iranian owner, a collision, held
    stale = _volakas_index()
    stale.add_surface_alias("spider_red", "Volakas Marble")
    held = VariationEngine(stale, auto_accept=92, review_floor=84).match("Volakas Marble", block_type="marble",
                                                                         block_origin="IR")
    assert held.cid is None and held.method.endswith("_collision")
    fresh = VariationEngine(_volakas_index(), auto_accept=92, review_floor=84).match(
        "Volakas Marble", block_type="marble", block_origin="IR")
    assert fresh.cid == "volocas"
