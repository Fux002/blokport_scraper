"""M5 DoD: a curated set of near-miss names resolves to the right variation,
false matches are rejected, and a deliberately unknown variety lands in the gap
queue. Plus Stage 5 membership: variety-authoritative type, snapping, and the
missing_leaf_child / missing_variation routing.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex, build_variation_index
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation, reconcile_tree


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def slab_engine(ref):
    index = build_variation_index(ref.variants["slab"], ref.backbone)
    return VariationEngine(index, auto_accept=92, review_floor=84)


# --- positive near-miss resolution -------------------------------------------
# These assert the correct VARIETY is found at a confident tier. The exact tier
# string is not asserted: in the live 12k-variant reference a name can map to
# several ids (e.g. "Afyon White" appears many times), so the engine resolves it
# at the fuzzy/phonetic tier rather than a single-id exact hit. The contract is
# "the right variety, confidently", not "via a specific tier".
# 'Aspen White' is deliberately NOT here: it is a genuinely AMBIGUOUS alias, shared by three varieties
# across two types (Granite 'Indian Aspen White', Marble 'Afyon White', Marble 'Afyon White Billur'), so
# without a type block no single answer is correct -- the real pipeline disambiguates it by the product's
# scraped type, not by name alone (see test_cross_type_alias_needs_block below). The cases here are names
# that resolve to ONE variety by name: an exact canonical name, a typo, a spacing variant, a translation
# alias. Each must prefer the variety NAMED closest to the query over one that merely carries it as an alias.
@pytest.mark.parametrize(
    "query,expected_name",
    [
        ("Arabescato", "Arabescato"),   # exact canonical name beats 'White Ornamental' (alias 'Arabescato')
        ("AfyonWhite", "Afyon White"),  # spacing
        ("Arabescatto", "Arabescato"),  # spelling-by-ear typo, beats an alias-only namesake
        ("Afyon Beyazi", "Afyon White"),  # translation alias (single owner)
    ],
)
def test_near_miss_resolves(slab_engine, query, expected_name):
    match = slab_engine.match(query)
    assert match.canonical == expected_name
    assert match.cid is not None
    assert match.method not in ("review", "no_candidate")  # confidently resolved


def test_cross_type_alias_needs_block(slab_engine):
    """'Aspen White' is an alias of a Granite AND a Marble variety. WITHOUT a type it is ambiguous, so it
    must NOT be attributed to a specific stone by name alone (never-guess). WITH the product's type as a
    block, it resolves to the variety of that type -- the real pipeline always passes block_type."""
    granite = slab_engine.match("Aspen White", block_type="Granite")
    assert granite.canonical == "Indian Aspen White"
    marble = slab_engine.match("Aspen White", block_type="Marble")
    assert marble.canonical in ("Afyon White", "Afyon White Billur")


def test_projection_tiers_work_in_isolation():
    """The deterministic projection tiers still fire when a name is unambiguous
    (a single id), proven on a controlled index so the live reference's duplicate
    names do not mask them."""
    index = CandidateIndex()
    index.add("v1", "Mont Blanc", surfaces=["Montblanc Quartzite"], block_type="quartzite")
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    assert engine.match("MontBlanc").method.startswith("projection")  # compact
    assert engine.match("Blanc Mont").method == "projection_tokenset"  # word order


def test_unknown_variety_lands_in_gap_queue(slab_engine):
    match = slab_engine.match("Zzytronic Nonexistent Stone")
    assert match.cid is None
    assert match.method in ("no_candidate", "review")


# --- false-positive rejection (the negatives that matter most) ----------------
def test_token_set_false_positive_is_rejected():
    """Crystal Frost must not collapse to Crystal (the token_set trap)."""
    index = CandidateIndex()
    index.add("c1", "Crystal", surfaces=[], block_type="quartz")
    index.add("c2", "Crystal Frost", surfaces=[], block_type="quartz")
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    # an exact name still resolves
    assert engine.match("Crystal Frost").cid == "c2"
    # a bare 'Crystal' must resolve to Crystal, not Crystal Frost
    assert engine.match("Crystal").cid == "c1"
    # a longer query that only shares the generic token must not snap to Crystal
    match = engine.match("Crystal Tempest Black")
    assert match.cid != "c1" or match.canonical != "Crystal"


def test_single_same_name_candidate_of_a_foreign_type_does_not_bind():
    """(type, name) is the identity. 'Absolute Black' scraped as Marble must NOT bind to the ONE existing
    'Absolute Black' (Granite) on name alone -- else an operator minting it as a NEW type is silently
    swallowed onto the Granite variety and never reaches the mint path (the real Absolute Black/Amazon Marble
    bug). The type gate the multi-candidate + fuzzy tiers already apply must apply here too. A matching or an
    absent scraped type still binds (the variety is the same / there is no type to contradict)."""
    index = CandidateIndex()
    index.add("g1", "Absolute Black", surfaces=[], block_type="granite")
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    assert engine.match("Absolute Black", block_type="marble").cid is None   # contradicts -> no bind (mintable)
    assert engine.match("Absolute Black", block_type="granite").cid == "g1"  # matches -> binds
    assert engine.match("Absolute Black").cid == "g1"                        # type-less -> binds


def test_two_same_name_candidates_of_a_foreign_type_still_do_not_bind():
    """The multi-candidate case is unchanged: 2+ same-name varieties of types that the scrape matches none of
    stay unbound (route to review / mint the new type), never collapsed onto an arbitrary wrong-type sibling."""
    index = CandidateIndex()
    index.add("g1", "Absolute Black", surfaces=[], block_type="granite")
    index.add("o1", "Absolute Black", surfaces=[], block_type="onyx")
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    assert engine.match("Absolute Black", block_type="marble").cid is None


def test_color_blocking_prevents_cross_color_match():
    index = CandidateIndex()
    index.add("b1", "Pearl", surfaces=[], block_type="granite", block_colors={"Black"})
    index.add("b2", "Pearl", surfaces=[], block_type="marble", block_colors={"White"})
    # two different ids share the surface 'Pearl'; an exact lookup is ambiguous and
    # must not silently pick one. The engine returns no single exact hit.
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    match = engine.match("Pearl", block_type="granite", block_color="Black")
    # blocked fuzzy should prefer the granite/Black candidate
    assert match.cid in (None, "b1")


# --- Stage 4 integration: generic descriptor with no variety -> gap ----------
def test_empty_variety_match_key_routes_to_gap(ref):
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="marenostone", surrogate_key="x", raw_name="Cream Marble Tile",
                       variety_match_key="", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id is None
    assert any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)


# --- Stage 5 membership -------------------------------------------------------
def test_type_overridden_by_variety(ref):
    # The bound variation's Key is the type authority: the matcher bound the MARBLE Arabescato
    # (variation_key type slug 'marble'), so a scraped type_name of Granite is overridden to Marble.
    # A matched row always carries variation_key (match_variation sets id + key together).
    row = CanonicalRow(
        src_site="polonine", surrogate_key="x",
        variation_id="vid",
        variation_key="slab_marble_arabescato_00000000-0000-0000-0000-000000000000",
        variation_name="Arabescato",
        type_name="Granite", color_name="White", finish_name="Polished", quality_name="A",
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    # Arabescato is a marble in the backbone; type overridden to the bound variation's type
    assert row.type_name == "Marble"
    assert any(f.code == FlagCode.type_overridden_by_variety for f in row.review_flags)


def test_type_name_fallback_when_key_carries_no_known_type_slug(ref):
    # F2 / State 2: the variation bound, but its Key carries no RECOGNIZED type slug, so the type falls back
    # to the name-derived type_name -- which is NOT variation-authoritative. The value is kept (best guess)
    # but its provenance must say 'type_name_fallback', so derive_origin will NOT trust it for the curated
    # (name, type) origin lookup (the confidently-wrong homonym-origin bug). No override flag: nothing was
    # overridden by an authoritative Key.
    row = CanonicalRow(
        src_site="polonine", surrogate_key="x",
        variation_id="vid",
        variation_key="slab_notarealtype_azul_white_00000000-0000-0000-0000-000000000000",
        variation_name="Azul White",
        type_name="Quartzite", color_name="White", finish_name="Polished", quality_name="A",
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    assert row.type_name == "Quartzite"                 # name-derived type kept as the best guess
    assert row.type_method == "type_name_fallback"      # ...but explicitly NOT variety_authoritative
    assert not any(f.code == FlagCode.type_overridden_by_variety for f in row.review_flags)


def test_membership_validates_real_variety(ref):
    # Verde Ubatuba: Granite, Green, Polished, A  (seen resolving in the spine)
    variety = ref.backbone.lookup("Verde Ubatuba")
    assert variety is not None
    row = CanonicalRow(
        src_site="polonine", surrogate_key="y",
        variation_id="vid", variation_name="Verde Ubatuba",
        type_name=variety.stone_type,
        color_name=variety.colors[0],
        finish_name=variety.finishes[0],
        quality_name=variety.qualities[0],
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    assert stats.validated == 1
    assert row.type_id is not None and row.category_pcat_id is not None


def test_colour_filled_from_variety_when_scrape_omits_it(ref):
    # a variety with exactly one allowed colour fills it at high confidence
    single = next(
        v for vs in ref.backbone.by_norm_name.values() for v in vs
        if len(v.colors) == 1 and v.finishes and v.qualities
    )
    row = CanonicalRow(
        src_site="zucchi", surrogate_key="z1",
        variation_id="vid", variation_name=single.variant,
        type_name=single.stone_type, color_name=None,  # scrape had no colour
        finish_name=single.finishes[0], quality_name=single.qualities[0],
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    assert row.color_name == single.colors[0]
    assert row.color_method == "from_variety"
    assert row.color_id is not None  # now importable
    assert stats.validated == 1


def test_colour_from_multicolour_variety_is_flagged(ref):
    multi = next((v for v in ref.backbone.by_norm_name.get("antique blue", []) if len(v.colors) > 1), None)
    if multi is None:
        pytest.skip("no multi-colour variety available in this reference")
    row = CanonicalRow(
        src_site="zucchi", surrogate_key="z2",
        variation_id="vid", variation_name=multi.variant,
        type_name=multi.stone_type, color_name=None,
        finish_name=multi.finishes[0], quality_name=multi.qualities[0],
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    assert row.color_name == multi.colors[0]  # primary
    assert row.color_method == "from_variety_primary"
    assert any(f.code == FlagCode.attr_from_variety for f in row.review_flags)


def test_scrape_colour_is_not_overridden_by_variety(ref):
    variety = ref.backbone.lookup("Verde Ubatuba")
    row = CanonicalRow(
        src_site="x", surrogate_key="z3",
        variation_id="vid", variation_name="Verde Ubatuba",
        type_name=variety.stone_type, color_name=variety.colors[0],
        finish_name=variety.finishes[0], quality_name=variety.qualities[0],
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    # the scrape supplied a valid colour; method stays as-is, not from_variety
    assert row.color_method != "from_variety"


def test_disallowed_color_routes_missing_leaf(ref):
    variety = ref.backbone.lookup("Verde Ubatuba")
    bad_color = "Pink" if "Pink" not in variety.colors else "Orange"
    row = CanonicalRow(
        src_site="polonine", surrogate_key="z",
        variation_id="vid", variation_name="Verde Ubatuba",
        type_name=variety.stone_type, color_name=bad_color,
        finish_name=variety.finishes[0], quality_name=variety.qualities[0],
    )
    stats = reconcile_tree.ReconcileStats()
    reconcile_tree.reconcile_row(row, ref, stats)
    assert any(g.gap_kind == GapKind.missing_leaf_child for g in row.tree_gaps)
