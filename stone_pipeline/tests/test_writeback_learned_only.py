"""The alias write-back keeps only learned GUESSES (owner, 2026-11-05).

A bind the seed already decides (exact, exact narrowed by name / type / colour / origin, projection, override)
is re-derived on every run, so saving it adds only stickiness: the saved pick outlived a later seed
correction. Prod: four marenostone 'Volakas Marble' slabs bound to Spider Red by origin (exact_origin) and the
pick was saved, so moving the seed alias to Volocas alone would have left them colliding. Hermetic: a
hand-built index and a tmp file, runs in CI.
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
            by_id={"a": SimpleNamespace(key="slab_granite_kashmir_golda_1", archived=False)})}}),
        engines={"slab": VariationEngine(idx, auto_accept=90, review_floor=80)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Goldx",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id == "a" and row.variation_method == "fuzzy"
    assert stage.writeback.aliases == [("a", "Kashmir Goldx", "fuzzy")]


def _volakas_index():
    idx = CandidateIndex()
    idx.add("spider_red", "Spider Red", surfaces=["Red Spider"], block_type="marble", origins={"CN", "GR", "IR", "TR"})
    idx.add("volocas", "Volocas", surfaces=["Persian Volakas", "Volakas Marble"], block_type="marble",
            origins={"IR"})
    idx.add("jazz_white", "Jazz White", surfaces=["Volakas Marble"], block_type="marble", origins={"GR"})
    return idx


def test_an_old_saved_origin_pick_no_longer_binds(tmp_path):
    path = tmp_path / "alias_writeback.csv"
    path.write_text("variation_id,alias,method\r\nspider_red,Volakas Marble,exact_origin\r\n", encoding="utf-8")
    idx = _volakas_index()
    match_variation._apply_writeback(idx, wb.load_alias_writeback(path))
    m = VariationEngine(idx, auto_accept=92, review_floor=84).match("Volakas Marble", block_type="marble",
                                                                    block_origin="IR")
    assert m.cid == "volocas"
    # the saved pick, had it been applied, makes Spider Red a second Iranian owner: a collision, held
    stale = _volakas_index()
    match_variation._apply_writeback(stale, [("spider_red", "Volakas Marble")])
    held = VariationEngine(stale, auto_accept=92, review_floor=84).match("Volakas Marble", block_type="marble",
                                                                         block_origin="IR")
    assert held.cid is None and held.method.endswith("_collision")


def test_the_file_heals_on_the_next_flush_and_then_stays_untouched(tmp_path):
    path = tmp_path / "alias_writeback.csv"
    path.write_text("variation_id,alias,method\r\n"
                    "spider_red,Volakas Marble,exact_origin\r\n"
                    "v1,Kashmir Goldx,fuzzy\r\n"
                    "v2,Some Spelling,clean_variety_exact\r\n", encoding="utf-8")
    assert wb.WriteBack().flush(path) == 0                     # nothing new learned, but the file is healed
    assert wb.load_alias_writeback_records(path) == [("v1", "Kashmir Goldx", "fuzzy")]
    before = path.read_bytes()
    assert wb.WriteBack().flush(path) == 0
    assert path.read_bytes() == before                         # healed file: a re-run is a no-op


def test_a_non_learned_alias_handed_to_flush_is_not_written(tmp_path):
    path = tmp_path / "alias_writeback.csv"
    w = wb.WriteBack()
    w.add_alias("v1", "Narrowed Pick", method="exact_origin")
    assert w.flush(path) == 0 and not path.exists()
