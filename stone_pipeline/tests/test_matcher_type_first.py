"""The matcher narrows by TYPE before anything else, and a guess between two stones is never made.

Variety identity is (type, name). Two defects let a product bind to a stone the evidence did not name:

  1. Own-name narrowing ran BEFORE type narrowing. A WRONG-type variety owning the queried name discarded the
     right-type varieties that carry it as an alias; the type step then left nothing and the row fell to the
     fuzzy tier, which bound one arbitrarily (prod seed: 'Anatolia Black' Marble -> Nero Marquina), and the
     alias write-back learned it. Type comes first; identity-beats-alias applies inside the type.
  2. A fuzzy tie between DIFFERENT varieties that origin could not break returned the first in file order.
     A tie between two different stones goes to review, whatever the file order.

The real-seed cases read only committed files.
"""
from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex, build_variation_index
from stone_pipeline.reference.loaders.backbone import load_backbone
from stone_pipeline.reference.loaders.identity import type_slug_from_key
from stone_pipeline.reference.loaders.origins import load_origin_map
from stone_pipeline.reference.loaders.variants import load_variants
from stone_pipeline.state.writeback import is_learned
from stone_pipeline.tests.test_seed_alias_ownership import ROOT

T = SETTINGS.thresholds


def _engine(idx: CandidateIndex) -> VariationEngine:
    return VariationEngine(idx, T.variation_auto_accept, T.variation_review_floor)


# --- 1. type first, then identity beats alias ----------------------------------------------------------
def _taj_mahal(with_marble_twin: bool) -> CandidateIndex:
    idx = CandidateIndex()
    if with_marble_twin:
        idx.add("m", "Taj Mahal", [], block_type="Marble")
    idx.add("q1", "Taj Mahal Gold", ["Taj Mahal"], block_type="Quartzite")
    idx.add("q2", "Taj Mahal Classic", ["Taj Mahal"], block_type="Quartzite")
    return idx


@pytest.mark.parametrize("with_marble_twin", [False, True])
def test_a_wrong_type_name_owner_does_not_hide_the_right_type_alias_owners(with_marble_twin):
    # Two quartzites carry 'Taj Mahal' as an alias; a MARBLE is named 'Taj Mahal'. A quartzite listing is a
    # same-type collision of the two quartzites, held for review, exactly as when the marble does not exist.
    m = _engine(_taj_mahal(with_marble_twin)).match("Taj Mahal", block_type="Quartzite")
    assert m.cid is None and m.method.endswith("_collision")
    assert {c for c, _n, _s in m.candidates} == {"q1", "q2"}
    assert not is_learned(m.method)


def test_the_name_owner_still_wins_inside_its_own_type():
    # identity beats alias is kept, applied within the type: the marble 'Taj Mahal' is the marble listing
    m = _engine(_taj_mahal(True)).match("Taj Mahal", block_type="Marble")
    assert m.cid == "m" and m.method == "exact_blocked"


def test_identity_beats_alias_among_same_type_candidates():
    idx = CandidateIndex()
    idx.add("arab", "Arabescato", [], block_type="Marble")
    idx.add("orn", "White Ornamental", ["Arabescato"], block_type="Marble")
    idx.add("q", "Arabescato", [], block_type="Quartzite")
    m = _engine(idx).match("Arabescato", block_type="Marble")
    assert m.cid == "arab" and m.method == "exact_name_blocked"


@pytest.fixture(scope="module")
def slab_engine():
    table = load_variants(ROOT / "from_medusa/production/variants_export_base.csv")
    index = build_variation_index(table, load_backbone(), load_origin_map())
    return _engine(index), table


def test_seed_anatolia_black_marble_is_a_held_collision_never_nero_marquina(slab_engine):
    # Seed: 'Anatolia Black' is the NAME of a granite and an ALIAS of three marbles (Toros Black, Black Mango,
    # Nero Marquina). A marble listing is a marble collision for review, not a fuzzy pick.
    eng, table = slab_engine
    m = eng.match("Anatolia Black", block_type="Marble")
    assert m.cid is None and m.method.endswith("_collision")
    owners = {type_slug_from_key(table.by_id[c].key) for c, _n, _s in m.candidates}
    assert owners == {"marble"}


def test_seed_anatolia_black_granite_still_binds_its_name_owner(slab_engine):
    eng, table = slab_engine
    m = eng.match("Anatolia Black", block_type="Granite")
    assert m.cid is not None and table.by_id[m.cid].key.startswith("granite__anatolia_black_")


# --- 2. a fuzzy tie between two different stones goes to review -----------------------------------------
_ARABESCATO = {"a": ("Arabescato Corchia", ["Arabescato"]), "b": ("Arabescato Vagli", ["Arabescato"])}


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_a_fuzzy_tie_between_two_different_stones_is_reviewed_in_any_file_order(order):
    idx = CandidateIndex()
    for cid in order:
        name, aliases = _ARABESCATO[cid]
        idx.add(cid, name, aliases, block_type="Marble")
    m = _engine(idx).match("Arabescatto", block_type="Marble")
    assert m.cid is None and m.method == "review"
    assert {c for c, _n, _s in m.candidates[:2]} == {"a", "b"}
    assert not m.ambiguous          # distinct stones: a stronger raw spelling may still name one of them
    assert not is_learned(m.method)


def test_a_fuzzy_tie_origin_breaks_still_binds():
    idx = CandidateIndex()
    idx.add("a", "Arabescato Corchia", ["Arabescato"], block_type="Marble", origins={"IT"})
    idx.add("b", "Arabescato Vagli", ["Arabescato"], block_type="Marble", origins={"TR"})
    m = _engine(idx).match("Arabescatox", block_type="Marble", block_origin="TR")
    assert m.cid == "b" and m.method == "fuzzy_origin"


def test_a_same_name_duplicate_tie_is_still_an_ambiguous_review():
    idx = CandidateIndex()
    idx.add("d1", "Calacatta Gold", [], block_type="Marble")
    idx.add("d2", "Calacatta Gold", [], block_type="Marble")
    m = _engine(idx).match("Calacatta Golde", block_type="Marble")
    assert m.cid is None and m.method == "review" and m.ambiguous


def test_a_lone_fuzzy_winner_still_binds():
    idx = CandidateIndex()
    idx.add("c", "Bianco Carrara", [], block_type="Marble")
    m = _engine(idx).match("Bianco Carrara X", block_type="Marble")
    assert m.cid == "c" and m.method == "fuzzy"


def _calacatta(origins: tuple[set, set, set]) -> CandidateIndex:
    idx = CandidateIndex()
    idx.add("d1", "Calacatta Gold", [], block_type="Marble", origins=origins[0])
    idx.add("d2", "Calacatta Gold", [], block_type="Marble", origins=origins[1])
    idx.add("g", "Calacatta Golf", [], block_type="Marble", origins=origins[2])
    return idx


def test_a_tie_origin_narrows_away_from_the_duplicate_is_not_ambiguous():
    # three tied (two same-name 'Calacatta Gold' + 'Calacatta Golf'); Italian origin keeps one Gold and the
    # Golf: what is left is a tie between DISTINCT stones, so it is a review but not flagged ambiguous (the
    # ambiguity is read from the origin-narrowed set, as the exact tier does)
    m = _engine(_calacatta(({"IT"}, {"IN"}, {"IT"}))).match("Calacatta Golx", block_type="Marble", block_origin="IT")
    assert m.cid is None and m.method == "review" and not m.ambiguous


def test_a_tie_origin_narrows_to_the_duplicate_is_ambiguous():
    m = _engine(_calacatta(({"IT"}, {"IT"}, {"TR"}))).match("Calacatta Golx", block_type="Marble", block_origin="IT")
    assert m.cid is None and m.method == "review" and m.ambiguous


# The short-name guard applies to the stone that origin picks, whatever the file order: 'Glacier' (one word) never
# wins a three-word query, while the two-word 'Arctic Snow' may. Both carry the same alias, so they tie.
_POLAR = {"a": ("Arctic Snow", ["Polar Iceberg White"], {"IT"}), "b": ("Glacier", ["Polar Iceberg White"], {"TR"})}


def _polar(order) -> CandidateIndex:
    idx = CandidateIndex()
    for cid in order:
        name, aliases, origins = _POLAR[cid]
        idx.add(cid, name, aliases, block_type="Marble", origins=origins)
    return idx


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_origin_never_binds_a_tied_stone_the_short_name_guard_rejects(order):
    m = _engine(_polar(order)).match("Polar Iceberg Whitx", block_type="Marble", block_origin="TR")
    assert m.cid is None and m.method == "review"


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_origin_binds_a_tied_stone_that_passes_the_guard_in_any_file_order(order):
    m = _engine(_polar(order)).match("Polar Iceberg Whitx", block_type="Marble", block_origin="IT")
    assert m.cid == "a" and m.method == "fuzzy_origin"
