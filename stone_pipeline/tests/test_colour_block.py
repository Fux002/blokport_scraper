"""Colour must never block variety IDENTIFICATION. Identity is (type, name) and no two varieties share it,
so the TYPE identifies the variety; colour is a product attribute used only to break a genuine same-type tie.
A scraped colour the catalog does not yet list for a variety is a new-combination gap to APPROVE downstream
(a leaf-gap), never a reason to fail identification. Controlled indexes only -- runs in CI (no export needed).
"""

from __future__ import annotations

from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex


def _engine(index: CandidateIndex) -> VariationEngine:
    return VariationEngine(index, auto_accept=92, review_floor=84)


def test_colour_does_not_block_a_type_identified_variety():
    # Matterhorn exists as Dolomite (Grey/Blue) AND Marble (White/Gold). A scrape typed Dolomite but coloured
    # White must resolve to Matterhorn DOLOMITE (type identifies it); the White colour is a new value to
    # approve downstream. Before the fix, type+colour blocked together: White dropped the Dolomite candidate,
    # type dropped the Marble one, and it gapped ambiguous. This is the 'Matterhorn Dolomite White' prod case.
    idx = CandidateIndex()
    idx.add("dolo", "Matterhorn", surfaces=[], block_type="dolomite", block_colors={"Grey", "Blue"})
    idx.add("marb", "Matterhorn", surfaces=[], block_type="marble", block_colors={"White", "Gold"})
    m = _engine(idx).match("Matterhorn", block_type="Dolomite", block_color="White")
    assert m.cid == "dolo", f"type must identify the variety; colour must not block it (got {m.cid}, {m.method})"


def test_colour_still_breaks_a_genuine_same_type_tie():
    # When two candidates survive the type block (a same-(type,name) duplicate), colour is still the tiebreaker.
    idx = CandidateIndex()
    idx.add("a", "Pearl", surfaces=[], block_type="granite", block_colors={"Black"})
    idx.add("b", "Pearl", surfaces=[], block_type="granite", block_colors={"White"})
    m = _engine(idx).match("Pearl", block_type="granite", block_color="Black")
    assert m.cid == "a", f"colour must break a same-type tie (got {m.cid}, {m.method})"


def test_type_still_disambiguates_same_name_varieties():
    # The original guard: Pearl granite/Black vs Pearl marble/White -> type picks the granite one.
    idx = CandidateIndex()
    idx.add("g", "Pearl", surfaces=[], block_type="granite", block_colors={"Black"})
    idx.add("m", "Pearl", surfaces=[], block_type="marble", block_colors={"White"})
    assert _engine(idx).match("Pearl", block_type="granite", block_color="Black").cid == "g"


def test_a_new_type_on_a_multitype_name_still_holds_and_colour_cannot_cross_it():
    # A scraped TYPE that matches NONE of the same-name varieties is a NEW type -> stay ambiguous for review
    # (mint the new type); colour must NOT resolve it to a wrong-type sibling.
    idx = CandidateIndex()
    idx.add("dolo", "Matterhorn", surfaces=[], block_type="dolomite", block_colors={"Grey"})
    idx.add("marb", "Matterhorn", surfaces=[], block_type="marble", block_colors={"White"})
    m = _engine(idx).match("Matterhorn", block_type="Quartzite", block_color="White")
    assert m.cid is None, f"a new type must not resolve to a sibling by colour (got {m.cid}, {m.method})"
