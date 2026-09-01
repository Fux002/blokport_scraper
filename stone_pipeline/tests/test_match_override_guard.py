"""L#46: a manual variation_id override is honoured only when it is a real candidate in THIS branch.

A stale pre-remint id, or a slab id fed on the block branch, is NOT in the branch's candidate index and
resolves to no stable Key. The old code committed it anyway -- variation_id=forced, variation_key=None, at
high confidence "override" -- an orphan link that bypasses the gap/review path and FK-fails on ack. The
override must be ignored and the row must fall through to the normal match/gap path.

Self-contained (hand-built engine + stub ref), so it runs in CI (the fixture-based override tests are
data-gated and never execute there).
"""

from __future__ import annotations

from types import SimpleNamespace

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import match_variation
from stone_pipeline.state.overrides import Overrides
from stone_pipeline.state.writeback import WriteBack


def _stage(override_id):
    idx = CandidateIndex()
    idx.add("v_real", "Carrara", surfaces=["Carrara"], block_type="marble")
    eng = VariationEngine(idx, auto_accept=92, review_floor=84)
    ref = SimpleNamespace(
        overrides=Overrides(by_key={("polonine", "620"): {"variation_id": override_id}}),
        variants={"slab": SimpleNamespace(by_id={"v_real": SimpleNamespace(key="slab_marble_carrara_1")})},
        variety_seed_types={},   # a real ReferenceData always carries this; the stub must too
    )
    return match_variation.VariationStage(ref=ref, engines={"slab": eng}, writeback=WriteBack())


def _row():
    return CanonicalRow(src_site="polonine", surrogate_key="620",
                        variety_match_key="UNKNOWN STONE", raw_format="Slab")


def test_override_id_not_a_candidate_is_ignored():
    row = _row()
    _stage("vid_bogus_not_a_candidate").resolve_row(row)
    assert row.variation_method != "override"                 # bogus override did not win
    assert row.variation_id != "vid_bogus_not_a_candidate"    # no orphan id committed
    assert row.variation_id is None                           # UNKNOWN STONE -> gapped (fall-through)


def test_override_id_that_is_a_candidate_still_wins():
    # the guard must not break the legitimate case: a real candidate id is honoured with its stable Key.
    row = _row()
    _stage("v_real").resolve_row(row)
    assert row.variation_method == "override"
    assert row.variation_id == "v_real"
    assert row.variation_key == "slab_marble_carrara_1"        # the stable Key, not None
