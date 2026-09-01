"""Operator-type fallback in match_variation: a product whose SCRAPED type matches no existing variety of
its name binds to the operator-MINTED (name, type) instead of gapping -- the mint decision reaching the
PRODUCT so an operator-defined variant attaches exactly like a suggested one (its type then flows through
the same reconcile/derive/texture path). Self-contained (hand-built engine + stub ref), runs in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import match_variation
from stone_pipeline.state.overrides import Overrides
from stone_pipeline.state.writeback import WriteBack


def _stage(seed_types):
    idx = CandidateIndex()
    # 'Absolute Black' exists as a pre-existing Granite variety AND an operator-minted Agate one -- the
    # (type, name) atomic identity, two distinct varieties sharing the name.
    idx.add("v_granite", "Absolute Black", surfaces=["Absolute Black"], block_type="granite")
    idx.add("v_agate", "Absolute Black", surfaces=["Absolute Black"], block_type="agate")
    eng = VariationEngine(idx, auto_accept=92, review_floor=84)
    ref = SimpleNamespace(
        overrides=Overrides(by_key={}),
        variants={"slab": SimpleNamespace(by_id={
            "v_granite": SimpleNamespace(key="slab_granite_absolute_black_1"),
            "v_agate": SimpleNamespace(key="slab_agate_absolute_black_2")})},
        variety_seed_types=seed_types,
    )
    return match_variation.VariationStage(ref=ref, engines={"slab": eng}, writeback=WriteBack())


def _row(match_key, raw_type):
    return CanonicalRow(src_site="marenostone", surrogate_key="1",
                        variety_match_key=match_key, raw_type=raw_type, raw_format="Slab")


def test_contradicting_scraped_type_binds_to_the_operator_minted_variety():
    # scraped Marble matches neither Granite nor Agate; operator minted Absolute Black as Agate -> bind Agate.
    row = _row("Absolute Black", "Marble")
    _stage({"absolute black": "Agate"}).resolve_row(row)
    assert row.variation_id == "v_agate"
    assert row.variation_key == "slab_agate_absolute_black_2"


def test_contradicting_type_binds_even_when_the_query_carries_the_type_token():
    # the real supplier shape: the match key still carries the type word ('absolute black marble'), so the
    # clean variety only matches via the fuzzy tier -- the fallback re-matches across ALL tiers, so it binds.
    row = _row("Absolute Black Marble", "Marble")
    _stage({"absolute black": "Agate"}).resolve_row(row)
    assert row.variation_id == "v_agate"


def test_matching_scraped_type_is_never_overridden():
    # scraped Granite matches the existing Granite variety -> binds there; the Agate mint is NOT applied to it.
    row = _row("Absolute Black", "Granite")
    _stage({"absolute black": "Agate"}).resolve_row(row)
    assert row.variation_id == "v_granite"


def test_no_operator_decision_still_gaps():
    # no mint decision for this name -> no fallback -> the contradicting-type product gaps (unchanged).
    row = _row("Absolute Black", "Marble")
    _stage({}).resolve_row(row)
    assert row.variation_id is None


def test_typeless_scrape_is_disambiguated_by_the_operator_type():
    # a type-less scrape of a name that exists under several types is ambiguous alone; the operator's mint
    # decision resolves it to the operator-minted variety.
    row = _row("Absolute Black", "")
    _stage({"absolute black": "Agate"}).resolve_row(row)
    assert row.variation_id == "v_agate"
