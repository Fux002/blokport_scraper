"""Engine-level guards that stop a fuzzy/phonetic hit from silently attaching to the WRONG variety
(review findings F10/F13). These are the matcher's safety rails; pinned on a controlled synthetic index so
they cannot regress. Hermetic (no live reference), so they run in CI. No logic here -- only the guarantees.
"""

from __future__ import annotations

from stone_pipeline.matching.engine import VariationEngine, _colour_conflict
from stone_pipeline.matching.index import CandidateIndex


def test_colour_conflict_rejects_disagreeing_explicit_colours():
    # Colour differentiates same-base varieties (Andromeda White vs Andromeda Cream), so a fuzzy/phonetic
    # hit across an explicit colour disagreement is wrong and is rejected.
    assert _colour_conflict("Andromeda White", "Andromeda Cream") is True
    assert _colour_conflict("Star Galaxy Black", "Star Galaxy White") is True
    assert _colour_conflict("White Ornamental", "White Something") is False   # share 'white' -> no conflict
    assert _colour_conflict("Mont Blanc", "Mont Blancx") is False             # neither names a colour


def test_tied_canonical_homonym_routes_to_review_not_an_arbitrary_pick():
    # A same-name multi-type homonym (two ids sharing the CANONICAL name -- 'Calacatta Gold' as marble AND
    # dolomite_marble) that a typo fuzzy-matches at >= auto_accept must route to REVIEW, never auto-accept
    # one by arbitrary export-row order (guards F10: a confidently-wrong homonym pick).
    idx = CandidateIndex()
    idx.add("m1", "Calacatta Gold", surfaces=[], block_type="marble")
    idx.add("d1", "Calacatta Gold", surfaces=[], block_type="dolomite_marble")
    eng = VariationEngine(idx, auto_accept=92, review_floor=84)
    m = eng.match("Calacata Gold")                # one-letter typo, fuzzy ~96 -> tied on the shared canonical
    assert m.cid is None and m.method == "review"


def test_a_lone_typo_still_resolves_when_the_canonical_is_unique():
    # The tied guard must NOT block a genuine typo correction to a UNIQUE variety (no false HOLD).
    idx = CandidateIndex()
    idx.add("v1", "Bianco Carrara", surfaces=[], block_type="marble")
    eng = VariationEngine(idx, auto_accept=92, review_floor=84)
    m = eng.match("Bianco Carara")                # typo -> the one Bianco Carrara, confidently resolved
    assert m.cid == "v1" and m.method in ("fuzzy", "phonetic")   # a confident tier, not held for review
