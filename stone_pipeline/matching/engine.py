"""The shared matching engine (section 5A).

One alias-and-projection-aware tiered resolver. The attribute normalization
(Stage 3) and origin lookup use the vocabulary resolver here; the full
variation engine (Stage 4, section 5A.2 tiers 1 to 6) is also built here so a
single module backs all matched fields (section 5A.4). The semantic tier (tier 8)
is built here as a review-only suggester (gated by enable_semantic). Tier 7's residual
role is filled by the alias_resolver logistic model (Splink was retired).
"""

from __future__ import annotations

import re
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
    from stone_pipeline.adapters.tokens import known_values

    cset = {c.casefold() for c in known_values("color")}
    return {_COLOUR_CANON.get(t.casefold(), t.casefold())
            for t in proj.norm(text).split() if t.casefold() in cset}


def _colour_conflict(query: str, candidate: str) -> bool:
    """True when both the query and the candidate name an explicit colour and they
    disagree. Colour is the differentiator between same-base varieties (Andromeda
    White vs Andromeda Cream), so a fuzzy/phonetic hit across that line is wrong."""
    q, c = _colour_words(query), _colour_words(candidate)
    return bool(q and c and not (q & c))


# --- vocabulary resolver (Stage 3 attributes, section 5A.4) -------------------
# Separators inside one raw value. A COMBINATION ('+ & / and') joins the parts -> compose 'A and B'; a LIST
# ('| ,') is alternatives -> keep the primary. The two separator sets are defined ONCE here, so splitting
# into parts and testing for a combination can never drift apart. _JOIN_NOISE drops descriptive wrappers.
_COMBINE_SEP = r"\+|&|/|\band\b"
_LIST_SEP = r"\||,"
_JOIN_SPLIT = re.compile(rf"\s*(?:{_COMBINE_SEP}|{_LIST_SEP})\s*", re.IGNORECASE)
_COMBINE = re.compile(_COMBINE_SEP, re.IGNORECASE)
_JOIN_NOISE = re.compile(r"\b(?:dual|combination|combo|mixed|finish|finishes)\b|[-–]", re.IGNORECASE)


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
        # Only a vocab that already uses the 'X and Y' pattern (finishes) may compose a compound.
        self._has_compound_pattern = any(" and " in v.casefold() for v in self.canonical_values)

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

    def resolve_multi(self, raw_value: str) -> Resolution:
        """Resolve a value that joins several attributes ('Polished + Leather', 'Flamed | Leathered').
        Call after resolve() fails on the whole value. A combination join composes the vocab's 'A and B'
        (returned as `compound` if it exists, else suggested as `compound_suggest` when the vocab has the
        pattern); a list join is alternatives, so the primary is kept (`multi_value`). Compounds are only
        composed for a vocab that already has 'X and Y' values, so a compound colour is never invented."""
        stripped = _JOIN_NOISE.sub(" ", raw_value or "")
        parts = [p.strip() for p in _JOIN_SPLIT.split(stripped) if p.strip()]
        if len(parts) < 2:
            return Resolution(value=None, confidence=Confidence.none, method="unresolved")
        resolved: list[str] = []
        for part in parts:
            v = self.resolve(part).value
            if v is not None and v not in resolved:
                resolved.append(v)
        if not resolved:
            return Resolution(value=None, confidence=Confidence.none, method="unresolved")
        if len(resolved) >= 2 and self._has_compound_pattern and _COMBINE.search(stripped):
            for a, b in ((resolved[0], resolved[1]), (resolved[1], resolved[0])):
                canon = self._by_norm.get(proj.norm(f"{a} and {b}"))
                if canon:
                    return Resolution(value=canon, confidence=Confidence.medium, method="compound")
            return Resolution(value=None, confidence=Confidence.low, method="compound_suggest",
                              evidence={"best": f"{resolved[0]} and {resolved[1]}"})
        return Resolution(value=resolved[0], confidence=Confidence.low, method="multi_value")


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
        ambiguous_to_review: bool = False,
        query: str = "",
    ) -> Optional[VariationMatch]:
        # a single id resolves directly; several ids for the same surface (the
        # reference has duplicate names) are disambiguated by type/colour blocking,
        # so a real name like "White Travertine" still resolves cleanly
        # IDENTITY beats ALIAS membership: when the surface hits several varieties but the query is the
        # CANONICAL NAME of some of them, keep only those. 'Arabescato' the query is the variety named
        # 'Arabescato', not 'White Ornamental' (which merely carries 'Arabescato' as an alias) nor the
        # longer 'Arabescato Arni'. Only narrows the set; if none (query is an alias of all) or several
        # (same name across types) match by name, the block + ambiguity logic below still applies.
        if len(ids) > 1 and query:
            named = {cid for cid in ids
                     if (c := self._candidate(cid)) and proj.norm(c.canonical) == proj.norm(query)}
            if named:
                ids = named
                method = f"{method}_name"
        # Narrow by TYPE, which alone identifies the variety: identity is (type, name) and no two varieties
        # share it. Colour is a product ATTRIBUTE, never part of identity -- use it only to break a tie the
        # type left, NEVER to reject a type-identified variety. A scraped colour the catalog does not yet
        # list for that variety is a new-combination gap to APPROVE downstream (leaf-gap), not an identity
        # failure -- so 'Matterhorn Dolomite White' resolves to Matterhorn Dolomite (Grey/Blue) with White
        # surfaced as a new colour, instead of gapping ambiguous because White belongs to the Marble twin.
        if block_type:
            by_type = {cid for cid in ids if self._block_ok(cid, block_type, "")}
            if by_type:
                if by_type != ids:
                    method = f"{method}_blocked"
                ids = by_type
                if len(ids) > 1 and block_color:          # a genuine same-type tie -> colour breaks it
                    by_color = {cid for cid in ids if self._block_ok(cid, "", block_color)}
                    if len(by_color) == 1:
                        ids = by_color
            elif len(ids) == 1:
                # A SINGLE same-name candidate whose type the scrape CONTRADICTS is a NEW-type variety, not
                # this one -- (type, name) is the identity. Do NOT bind on name alone: the phonetic/fuzzy
                # tiers already drop a type-mismatched candidate, and the exact/projection tiers must too, or
                # an operator's decision to mint the name as a DIFFERENT type is silently swallowed onto the
                # lone existing variety (the row never reaches the mint path). Drop it so the row surfaces as
                # missing_variation and curate mints the operator's chosen type. (2+ same-name candidates of
                # a foreign type fall through to the ambiguous-duplicate review path below, unchanged.)
                ids = set()
            # else (2+ candidates, none of this type): keep them -> the ambiguous-duplicate path routes to review.
        elif len(ids) > 1 and block_color:                 # type-less scrape: colour is the only signal
            by_color = {cid for cid in ids if self._block_ok(cid, "", block_color)}
            if len(by_color) == 1:
                ids = by_color
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
        if len(ids) > 1 and ambiguous_to_review:
            # Only TRUE duplicates -- 2+ candidates with an IDENTICAL canonical name -- are genuinely
            # ambiguous (the fuzzy tier would score them equally and scored[0] picks one by arbitrary
            # export-row order). Route those to review. Candidates that merely SHARE this surface as an
            # alias but have DIFFERENT canonicals (e.g. 12 'Arabescato ...' varieties sharing the
            # 'Arabescato' family alias) are NOT ambiguous -- let block + fuzzy resolve them normally.
            canon = {}
            for cid in ids:
                c = self._candidate(cid)
                if c:
                    canon.setdefault(proj.norm(c.canonical), []).append(cid)
            dup = next((cids for cids in canon.values() if len(cids) > 1), None)
            if dup:
                cands = [(cid, c.canonical if (c := self._candidate(cid)) else None, 100.0) for cid in dup[:3]]
                return VariationMatch(None, None, Confidence.low, f"{method}_ambiguous", 100.0, cands)
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

        # tier 2: exact on norm (canonical + aliases). A genuine same-surface duplicate that blocking
        # can't separate routes to review here, not to an arbitrary fuzzy-tier pick.
        match = self._resolve_single(self.index.lookup_norm(query), "exact", Confidence.high,
                                     block_type, block_color, ambiguous_to_review=True, query=query)
        if match:
            return match

        # tier 3: projection-exact (compact, tokenset, deprefixed)
        for method, ids in (
            ("compact", self.index.lookup_compact(query)),
            ("tokenset", self.index.lookup_tokenset(query)),
            ("deprefixed", self.index.lookup_deprefixed(query)),
        ):
            match = self._resolve_single(ids, f"projection_{method}", Confidence.high,
                                         block_type, block_color, query=query)
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
        # Sort by score, then prefer a hit on the variety's CANONICAL name over a hit on one of its
        # aliases at the SAME score: 'Arabescatto' (typo) resolves to 'Arabescato' the variety, not to
        # 'White Ornamental' which merely carries 'Arabescato' as an alias. _fuzzy_score(query, canonical)
        # == best exactly when the canonical name is (one of) the best-scoring surface(s).
        scored.sort(key=lambda t: (t[2], _fuzzy_score(query, t[1]) >= t[2]), reverse=True)

        if scored:
            top_cid, top_name, top_score = scored[0]
            # length/token guard: a short generic candidate must not win
            if top_score >= self.auto_accept and self._passes_guard(query, top_name):
                # HOLD-not-guess: mirror the exact tier -- if the top fuzzy score is TIED between candidates
                # that share a canonical name (the same variety name under >1 stone type, e.g. 'Calacatta
                # Gold' marble vs dolomite_marble), auto-accepting picks one by arbitrary export-row order.
                # Route that duplicate to review. A shared family ALIAS across distinct varieties
                # ('Arabescato' -> many 'Arabescato ...') has different canonicals, so it still resolves;
                # a lone typo correction ('Bianco Carara' -> the one 'Bianco Carrara') is unaffected.
                tied_canon: dict[str, int] = {}
                for cid, _n, s in scored:
                    if s != top_score:
                        break
                    c = self._candidate(cid)
                    if c:
                        tied_canon[proj.norm(c.canonical)] = tied_canon.get(proj.norm(c.canonical), 0) + 1
                if any(n > 1 for n in tied_canon.values()):
                    return VariationMatch(None, None, Confidence.low, "review", top_score, scored[:3])
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
