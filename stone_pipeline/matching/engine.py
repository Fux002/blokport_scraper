"""The shared matching engine (section 5A).

One alias-and-projection-aware tiered resolver. The attribute normalization
(Stage 3) and origin lookup use the vocabulary resolver here; the full
variation engine (Stage 4, section 5A.2 tiers 1 to 6) is also built here so a
single module backs all matched fields (section 5A.4). Splink (tier 7) and the
semantic tier (tier 8) are deferred to M10.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import Resolution
from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.index import CandidateIndex


def _fuzzy_score(query: str, candidate: str) -> float:
    """token_sort_ratio and full ratio, take the max. Never token_set, which
    collapses 'Crystal Frost' into 'Crystal' (section 5A.2 tier 5)."""
    a, b = proj.norm(query), proj.norm(candidate)
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


# spelling/form variants of the SAME colour -- without canonicalizing these, 'Pietra Grey' and
# 'Pietra Gray' read as a colour CONFLICT and the same stone is wrongly split into two varieties.
_COLOUR_CANON = {"gray": "grey", "golden": "gold"}


def _colour_words(text: str) -> set[str]:
    from stone_pipeline.adapters.tokens import COLOR_TOKENS

    cset = {c.casefold() for c in COLOR_TOKENS}
    return {_COLOUR_CANON.get(t.casefold(), t.casefold())
            for t in proj.norm(text).split() if t.casefold() in cset}


def _colour_conflict(query: str, candidate: str) -> bool:
    """True when both the query and the candidate name an explicit colour and they
    disagree. Colour is the differentiator between same-base varieties (Andromeda
    White vs Andromeda Cream), so a fuzzy/phonetic hit across that line is wrong."""
    q, c = _colour_words(query), _colour_words(candidate)
    return bool(q and c and not (q & c))


# --- vocabulary resolver (Stage 3 attributes, section 5A.4) -------------------
@dataclass
class VocabResolver:
    """Normalize-then-lookup over a closed vocabulary: synonym, exact, fuzzy.
    Returns a Resolution carrying the canonical value as `value`."""

    vocab: str
    canonical_values: list[str]
    synonyms: dict[str, str]
    fuzzy_floor: float

    def __post_init__(self) -> None:
        self._by_norm = {proj.norm(v): v for v in self.canonical_values}

    def resolve(self, raw_value: str) -> Resolution:
        cleaned = proj.norm(raw_value)
        if not cleaned:
            return Resolution(value=None, confidence=Confidence.none, method="empty")

        # synonym dictionary (a 'none' target resolves to no value without error)
        if cleaned in self.synonyms:
            target = self.synonyms[cleaned]
            if target.casefold() == "none":
                return Resolution(value=None, confidence=Confidence.high, method="synonym_none")
            canon = self._by_norm.get(proj.norm(target), target)
            return Resolution(value=canon, confidence=Confidence.high, method="synonym")

        # exact against the canonical list
        if cleaned in self._by_norm:
            return Resolution(
                value=self._by_norm[cleaned], confidence=Confidence.high, method="exact"
            )

        # fuzzy fallback
        best_value: Optional[str] = None
        best_score = 0.0
        for value in self.canonical_values:
            score = _fuzzy_score(raw_value, value)
            if score > best_score:
                best_score = score
                best_value = value
        if best_value is not None and best_score >= self.fuzzy_floor:
            return Resolution(
                value=best_value,
                confidence=Confidence.medium,
                method="fuzzy",
                evidence={"score": round(best_score, 1)},
            )

        return Resolution(
            value=None,
            confidence=Confidence.none,
            method="unresolved",
            evidence={"best": best_value, "score": round(best_score, 1)},
        )


# --- variation engine (Stage 4 tiers 1 to 6, section 5A.2) --------------------
@dataclass
class VariationMatch:
    cid: Optional[str]
    canonical: Optional[str]
    confidence: Confidence
    method: str
    score: float
    candidates: list[tuple[str, str, float]]  # (cid, canonical, score) for review


class VariationEngine:
    """Category-scoped tiered resolver over one CandidateIndex (one branch).

    Blocks by branch already (the index is per branch), then by normalized type
    and colour in the fuzzy/overlap tiers, so a slab variety never matches a
    block id and Black Pearl granite never matches Black Pearl marble.
    """

    def __init__(self, index: CandidateIndex, auto_accept: float, review_floor: float, suggester=None):
        self.index = index
        self.auto_accept = auto_accept
        self.review_floor = review_floor
        # optional tier-8 semantic suggester (review only, never auto-accept)
        self.suggester = suggester

    def _candidate(self, cid: str):
        return self.index.candidates.get(cid)

    def _block_ok(self, cid: str, block_type: str, block_color: str) -> bool:
        cand = self._candidate(cid)
        if cand is None:
            return False
        nt, nc = proj.norm(block_type), proj.norm(block_color)
        if nt and cand.block_type and cand.block_type != nt:
            return False
        if nc and cand.block_colors and nc not in cand.block_colors:
            return False
        return True

    def _resolve_single(
        self,
        ids: set[str],
        method: str,
        confidence: Confidence,
        block_type: str = "",
        block_color: str = "",
    ) -> Optional[VariationMatch]:
        # a single id resolves directly; several ids for the same surface (the
        # reference has duplicate names) are disambiguated by type/colour blocking,
        # so a real name like "White Travertine" still resolves cleanly
        if len(ids) > 1 and (block_type or block_color):
            blocked = {cid for cid in ids if self._block_ok(cid, block_type, block_color)}
            if len(blocked) == 1:
                ids = blocked
                method = f"{method}_blocked"
        if len(ids) == 1:
            cid = next(iter(ids))
            cand = self._candidate(cid)
            return VariationMatch(
                cid=cid,
                canonical=cand.canonical if cand else None,
                confidence=confidence,
                method=method,
                score=100.0,
                candidates=[],
            )
        return None

    def match(
        self,
        query: str,
        block_type: str = "",
        block_color: str = "",
        overrides: Optional[dict[str, str]] = None,
    ) -> VariationMatch:
        empty = VariationMatch(None, None, Confidence.none, "no_candidate", 0.0, [])
        if not query or not query.strip():
            return empty

        # tier 1: override
        if overrides:
            key = proj.norm(query)
            if key in overrides:
                cid = overrides[key]
                cand = self._candidate(cid)
                return VariationMatch(cid, cand.canonical if cand else None, Confidence.high, "override", 100.0, [])

        # tier 2: exact on norm (canonical + aliases)
        match = self._resolve_single(self.index.lookup_norm(query), "exact", Confidence.high,
                                     block_type, block_color)
        if match:
            return match

        # tier 3: projection-exact (compact, tokenset, deprefixed)
        for method, ids in (
            ("compact", self.index.lookup_compact(query)),
            ("tokenset", self.index.lookup_tokenset(query)),
            ("deprefixed", self.index.lookup_deprefixed(query)),
        ):
            match = self._resolve_single(ids, f"projection_{method}", Confidence.high,
                                         block_type, block_color)
            if match:
                return match

        # tier 4: phonetic exact, blocked by type and colour (so a phonetic
        # collision never crosses stone types, e.g. Onyx vs Travertine) and
        # guarded by a char-similarity floor so it does not over-merge
        phon_ids = self.index.lookup_phonetic(query)
        guarded = []
        for cid in phon_ids:
            cand = self._candidate(cid)
            if cand is None:
                continue
            if not self._block_ok(cid, block_type, block_color):
                continue
            if _colour_conflict(query, cand.canonical):
                continue
            if proj.char_similarity(query, cand.canonical) >= 85.0:
                guarded.append(cid)
        match = self._resolve_single(set(guarded), "phonetic", Confidence.medium,
                                     block_type, block_color)
        if match:
            return match

        # tiers 5 and 6: fuzzy + overlap over blocked candidates
        nt, nc = proj.norm(block_type), proj.norm(block_color)
        scored: list[tuple[str, str, float]] = []
        for cid, cand in self.index.candidates.items():
            if nt and cand.block_type and cand.block_type != nt:
                continue
            if nc and cand.block_colors and nc not in cand.block_colors:
                continue
            if _colour_conflict(query, cand.canonical):
                continue  # query and variety name disagree on an explicit colour
            best = 0.0
            for surface in cand.surfaces:
                score = _fuzzy_score(query, surface)
                if score > best:
                    best = score
            if best > 0:
                scored.append((cid, cand.canonical, best))
        scored.sort(key=lambda t: t[2], reverse=True)

        if scored:
            top_cid, top_name, top_score = scored[0]
            # length/token guard: a short generic candidate must not win
            if top_score >= self.auto_accept and self._passes_guard(query, top_name):
                return VariationMatch(top_cid, top_name, Confidence.medium, "fuzzy", top_score, scored[:3])
            if top_score >= self.review_floor:
                return VariationMatch(None, None, Confidence.low, "review", top_score, scored[:3])

        # tier 8: semantic suggestion, review only, never auto-accept
        if self.suggester is not None:
            suggestion = self.suggester.suggest(query)
            if suggestion is not None:
                cid, name, score = suggestion
                if score >= self.review_floor:
                    return VariationMatch(None, None, Confidence.low, "semantic_review", score,
                                          [(cid, name, score), *scored[:2]])

        return VariationMatch(None, None, Confidence.none, "no_candidate", scored[0][2] if scored else 0.0, scored[:3])

    @staticmethod
    def _passes_guard(query: str, candidate: str) -> bool:
        """Prevent a short generic candidate from winning a fuzzy match against a
        longer query (section 5A.2 tier 5)."""
        q_tokens = proj.norm(query).split()
        c_tokens = proj.norm(candidate).split()
        if not q_tokens or not c_tokens:
            return False
        # candidate must not be drastically shorter (the Crystal Frost -> Crystal case)
        if len(c_tokens) < len(q_tokens) and len(c_tokens) == 1 and len(q_tokens) >= 2:
            return False
        return True
