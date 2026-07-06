"""M10: the semantic tier (tier 8) is config-gated, feeds review only, and never auto-accepts.
Tested offline with the deterministic hashing embedder.
"""

from __future__ import annotations

from stone_pipeline.config.settings import Confidence
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.matching.semantic import SemanticSuggester, hashing_embedder


def _index():
    index = CandidateIndex()
    index.add("c1", "Bianco Carrara", surfaces=["Carrara White"], block_type="marble")
    index.add("c2", "Nero Marquina", surfaces=[], block_type="marble")
    return index


def test_semantic_suggester_finds_nearest():
    index = _index()
    names, cids = [], []
    for cid, cand in index.candidates.items():
        for surface in cand.surfaces:
            names.append(surface)
            cids.append(cid)
    suggester = SemanticSuggester(names, cids, hashing_embedder())
    result = suggester.suggest("Carrara White")  # exact surface -> high
    assert result is not None
    cid, name, score = result
    assert cid == "c1" and score > 90


def test_engine_semantic_is_review_only_never_auto_accept():
    index = _index()
    names = [s for c in index.candidates.values() for s in c.surfaces]
    cids = [cid for cid, c in index.candidates.items() for _ in c.surfaces]
    suggester = SemanticSuggester(names, cids, hashing_embedder())
    engine = VariationEngine(index, auto_accept=92, review_floor=60, suggester=suggester)
    # a query that string tiers miss but is semantically close
    match = engine.match("Carrara White Marble")
    # never auto-accepted: cid stays None, routed to review with a suggestion
    assert match.cid is None
    assert match.method in ("semantic_review", "review", "no_candidate")
    if match.method == "semantic_review":
        assert match.confidence == Confidence.low
        assert match.candidates  # carries the suggestion for a human


def test_engine_without_suggester_is_unchanged():
    index = _index()
    engine = VariationEngine(index, auto_accept=92, review_floor=84, suggester=None)
    match = engine.match("Totally Unrelated Xyz")
    assert match.cid is None
    assert match.method == "no_candidate"
