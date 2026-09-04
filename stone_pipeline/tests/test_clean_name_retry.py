"""Clean-name retry in match_variation: a product whose match key carries the supplier's TYPE TOKEN
('Crystal White Granite') must still bind to the existing CANONICAL variety of that name ('Crystal White'
granite), not gap and re-mint a duplicate every produce.

The trap (the real prod 'Crystal White' / 'Golden Silver' false-mints): the type-token surface exists only
as an ALIAS on sibling same-type varieties (Bianco, Storen) -- never on the canonical variety of that exact
name, which lists no '<name> <type>' alias. So the exact tier sees several different-canonical candidates and
gaps. The stage retries with the type-STRIPPED name under the SAME scraped type; the engine's identity-beats-
alias narrowing then keeps the canonical owner and binds it. Identity stays (type, name): the scraped type
still blocks, so a name existing only under a DIFFERENT type still gaps and holds for review.

Self-contained (hand-built engine + stub ref), runs in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import match_variation
from stone_pipeline.state.overrides import Overrides
from stone_pipeline.state.writeback import WriteBack


def _stage(seed_types=None):
    idx = CandidateIndex()
    # the CANONICAL 'Crystal White' granite carries NO '<name> <type>' alias -- only its own name.
    idx.add("v_cw", "Crystal White", surfaces=[], block_type="granite")
    # sibling granite varieties that DO list the type-token spelling as an alias (the collision).
    idx.add("v_bianco", "Bianco", surfaces=["Crystal White Granite"], block_type="granite")
    idx.add("v_storen", "Storen", surfaces=["Crystal White Granite"], block_type="granite")
    # a name that exists ONLY under granite -- used to prove the scraped type still gates the retry.
    idx.add("v_ib", "Imperial Blue", surfaces=[], block_type="granite")
    eng = VariationEngine(idx, auto_accept=92, review_floor=84)
    ref = SimpleNamespace(
        overrides=Overrides(by_key={}),
        variants={"slab": SimpleNamespace(by_id={
            "v_cw": SimpleNamespace(key="slab_granite_crystal_white_1"),
            "v_bianco": SimpleNamespace(key="slab_granite_bianco_2"),
            "v_storen": SimpleNamespace(key="slab_granite_storen_3"),
            "v_ib": SimpleNamespace(key="slab_granite_imperial_blue_4")})},
        variety_seed_types=seed_types or {},
    )
    return match_variation.VariationStage(ref=ref, engines={"slab": eng}, writeback=WriteBack())


def _row(match_key, raw_type):
    return CanonicalRow(src_site="marenostone", surrogate_key="1",
                        variety_match_key=match_key, raw_type=raw_type, raw_format="Slab")


def test_type_token_query_binds_canonical_via_clean_retry():
    # 'Crystal White Granite' hits only the Bianco/Storen aliases (different canonicals) -> primary gaps;
    # the clean retry strips 'Granite' and binds the canonical Crystal White granite variety.
    row = _row("Crystal White Granite", "Granite")
    _stage().resolve_row(row)
    assert row.variation_id == "v_cw"
    assert row.variation_key == "slab_granite_crystal_white_1"
    assert row.variation_method.startswith("clean_variety_")
    assert not any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)


def test_clean_retry_respects_scraped_type_identity():
    # 'Imperial Blue Quartzite' cleans to 'Imperial Blue', but that variety exists only as GRANITE; the
    # retry blocks by the scraped Quartzite type, so it still gaps and holds (a genuinely new-type identity).
    row = _row("Imperial Blue Quartzite", "Quartzite")
    _stage().resolve_row(row)
    assert row.variation_id is None
    assert any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)


def test_no_type_token_is_unchanged():
    # a genuinely new name with no type token cleans to itself -> no retry -> gaps exactly as before.
    row = _row("Totally New Stone", "Granite")
    _stage().resolve_row(row)
    assert row.variation_id is None
    assert any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)


def test_matching_type_binds_at_primary_without_retry():
    # the already-clean 'Crystal White' scraped as granite binds directly at the exact tier (no retry needed);
    # the method is NOT a clean_variety_ retry.
    row = _row("Crystal White", "Granite")
    _stage().resolve_row(row)
    assert row.variation_id == "v_cw"
    assert not row.variation_method.startswith("clean_variety_")
