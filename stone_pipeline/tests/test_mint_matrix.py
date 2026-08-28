"""Mint decision matrix: for each CLASS of scraped input, assert EXACTLY what curate does and why.

One readable case per row of the truth table, so the mint contract is enumerated and proven, not assumed.
Deliberately small: it reuses the real reference (`load_all`) and the same CanonicalRow + missing_variation
gap every other mint test uses -- no new machinery.

Outcome vocabulary (the `curate.build_curation` result):
  MINT   a new variety Key appears in result.new_variants[branch]
  HOLD   the variety is surfaced in result.pending_confirm (operator must decide) and is NOT minted
  (ALIAS onto an existing variety is exercised in test_mint_type_authority / test_mint_resolve_no_duplicate.)

The rule the matrix guards: a variety mints ONLY as (branch, CANONICAL type, clean name), and ONLY once an
operator CONFIRMS it -- every new variety first HOLDS in pending_confirm (nothing is silently auto-minted).
Anything without a real type -- absent, non-canonical, or an un-vetted operator value -- also HOLDS, never
minting a garbage-slug Key. A CONFIRMED variety fans out to every active category, the same identity mints
once, and a same-name multi-type homonym surfaces as two distinct holds (not one).
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _scrape(name: str, raw_type: str = "", sk: str = "s1") -> CanonicalRow:
    """A product-backed scrape of `name` that surfaced as a missing_variation gap -- the mint entry point."""
    row = CanonicalRow(src_site="varsha", surrogate_key=sk, variety_match_key=name, raw_type=raw_type)
    row.add_gap(TreeGap(src_site="varsha", surrogate_key=sk, raw_name=name,
                        gap_kind=GapKind.missing_variation, nearest_existing="", nearest_score=0.0))
    return row


def _minted_keys(res, name_slug: str) -> list[str]:
    return [r["Key"] for b in ("slab", "block", "tile")
            for r in res.new_variants[b] if name_slug in r["Key"]]


def _is_held(res, name: str) -> bool:
    return any(p["variant"] == name for p in res.pending_confirm)


# --- the matrix -------------------------------------------------------------

def test_new_variety_with_a_canonical_type_holds_then_mints_when_confirmed(ref):
    # The confirm-gate: a genuinely-new name + canonical type does NOT auto-mint -- it HOLDS for one operator
    # confirmation (nothing is silently minted). Once CONFIRMED it mints, fanned out to slab+block+tile, each
    # Key carrying the type slug. This is the baseline definition of a correct mint under the review queue.
    from stone_pipeline.config import decisions_store
    held = curate.build_curation([_scrape("Novum Test Stone", raw_type="Granite")], ref)
    assert _is_held(held, "Novum Test Stone")                                    # new variety -> held, not minted
    assert not _minted_keys(held, "novum_test_stone")

    decisions_store.set_variety_decision("Novum Test Stone", "mint", seed_type="Granite")  # operator confirms
    res = curate.build_curation([_scrape("Novum Test Stone", raw_type="Granite")], ref)
    keys = _minted_keys(res, "novum_test_stone")
    assert sorted(k.split("_")[0] for k in keys) == ["block", "slab", "tile"]   # one per active category
    assert all("granite" in k for k in keys)                                     # typed Key everywhere
    assert not _is_held(res, "Novum Test Stone")                                 # confirmed -> no longer held


def test_typeless_scrape_holds_and_never_mints(ref):
    # no type from anywhere -> a variety cannot exist without one -> HOLD for the operator to assign it.
    res = curate.build_curation([_scrape("Karur Novel White", raw_type="")], ref)
    assert _is_held(res, "Karur Novel White")
    assert not _minted_keys(res, "karur_novel_white")


def test_noncanonical_type_holds_and_never_mints(ref):
    # a raw type that no synonym maps to a real Medusa type is NOT a type -> HOLD, never a garbage-slug Key.
    res = curate.build_curation([_scrape("Weird Novel Stone", raw_type="Notarealtype")], ref)
    assert _is_held(res, "Weird Novel Stone")
    assert not _minted_keys(res, "weird_novel_stone")
    assert not _minted_keys(res, "notarealtype")


def test_operator_seed_type_must_also_be_canonical(ref):
    # the operator resolves a type-less hold by assigning a seed_type; a NON-canonical seed value is rejected
    # the same way (defense in depth -- the :4200 API validates it too), so it stays held, never mints.
    from stone_pipeline.config import decisions_store
    decisions_store.set_variety_decision("Karur Novel White", "mint", seed_type="Notarealtype")
    res = curate.build_curation([_scrape("Karur Novel White", raw_type="")], ref)
    assert _is_held(res, "Karur Novel White")
    assert not _minted_keys(res, "karur_novel_white")


def test_two_scrapes_of_the_same_identity_mint_once(ref):
    # two rows cleaning to the SAME (type, name), once CONFIRMED, must not mint two Keys in a branch (dedup).
    from stone_pipeline.config import decisions_store
    decisions_store.set_variety_decision("Dupe Novel Stone", "mint", seed_type="Granite")
    rows = [_scrape("Dupe Novel Stone", raw_type="Granite", sk="a"),
            _scrape("Dupe Novel Stone", raw_type="Granite", sk="b")]
    res = curate.build_curation(rows, ref)
    slab = [r for r in res.new_variants["slab"] if "dupe_novel_stone" in r["Key"]]
    assert len(slab) == 1


def test_same_name_two_different_canonical_types_surface_as_two_distinct_holds(ref):
    # a real multi-type homonym (same name, two REAL types) is NOT a duplicate: it surfaces as TWO distinct
    # holds (one per type), never deduped into one -- each awaits its own confirm before minting its typed Key.
    rows = [_scrape("Homonym Novel", raw_type="Granite", sk="a"),
            _scrape("Homonym Novel", raw_type="Marble", sk="b")]
    res = curate.build_curation(rows, ref)
    holds = [p for p in res.pending_confirm if p.get("variant") == "Homonym Novel"]
    assert len(holds) == 2                                                       # two distinct varieties, not one
    assert not _minted_keys(res, "homonym_novel")                               # neither mints until confirmed
