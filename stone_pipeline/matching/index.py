"""Alias-aware candidate index (section 5A.1).

Build one index that flattens, for every candidate (a variation or a vocabulary
value), all of its known surface forms into lookup keys, each mapped back to the
candidate id. For each surface form precompute several normalized projections so
different classes of difference all become O(1) exact-lookup hits. Blocking
metadata (parent type, colour) is stored alongside each candidate for the fuzzy
and overlap tiers that need it.

The index is built once per run and shared across rows (section 13A.1), never
rebuilt per row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stone_pipeline.matching import projections as proj


@dataclass
class Candidate:
    cid: str  # the id this surface form maps to (variation id, or canonical vocab value)
    canonical: str  # the canonical display name
    block_type: str = ""  # normalized parent stone type, for blocking
    # a variety may allow several colours; blocking is set-membership, not equality
    block_colors: set[str] = field(default_factory=set)
    surfaces: list[str] = field(default_factory=list)  # all known surface forms


@dataclass
class CandidateIndex:
    candidates: dict[str, Candidate] = field(default_factory=dict)  # cid -> Candidate
    by_norm: dict[str, set[str]] = field(default_factory=dict)
    by_compact: dict[str, set[str]] = field(default_factory=dict)
    by_tokenset: dict[tuple, set[str]] = field(default_factory=dict)
    by_deprefixed: dict[str, set[str]] = field(default_factory=dict)
    by_phonetic: dict[tuple, set[str]] = field(default_factory=dict)

    def _add_surface(self, cid: str, surface: str) -> None:
        if not surface:
            return
        self.by_norm.setdefault(proj.norm(surface), set()).add(cid)
        self.by_compact.setdefault(proj.compact(surface), set()).add(cid)
        self.by_tokenset.setdefault(proj.tokenset(surface), set()).add(cid)
        self.by_deprefixed.setdefault(proj.deprefixed(surface), set()).add(cid)
        phon = proj.phonetic(surface)
        if phon:
            self.by_phonetic.setdefault(phon, set()).add(cid)

    def add(
        self,
        cid: str,
        canonical: str,
        surfaces: list[str],
        block_type: str = "",
        block_colors: set[str] | None = None,
    ) -> None:
        candidate = self.candidates.get(cid)
        if candidate is None:
            candidate = Candidate(
                cid=cid,
                canonical=canonical,
                block_type=proj.norm(block_type),
                block_colors={proj.norm(c) for c in (block_colors or set()) if c},
            )
            self.candidates[cid] = candidate
        for surface in [canonical, *surfaces]:
            if surface and surface not in candidate.surfaces:
                candidate.surfaces.append(surface)
                self._add_surface(cid, surface)

    def add_surface_alias(self, cid: str, surface: str) -> None:
        """Write-back: register a newly confirmed spelling (section 8.4)."""
        candidate = self.candidates.get(cid)
        if candidate is None or not surface:
            return
        if surface not in candidate.surfaces:
            candidate.surfaces.append(surface)
            self._add_surface(cid, surface)

    # --- exact-projection lookups (tiers 2 and 3) -----------------------------
    def lookup_norm(self, query: str) -> set[str]:
        return self.by_norm.get(proj.norm(query), set())

    def lookup_compact(self, query: str) -> set[str]:
        return self.by_compact.get(proj.compact(query), set())

    def lookup_tokenset(self, query: str) -> set[str]:
        return self.by_tokenset.get(proj.tokenset(query), set())

    def lookup_deprefixed(self, query: str) -> set[str]:
        return self.by_deprefixed.get(proj.deprefixed(query), set())

    def lookup_phonetic(self, query: str) -> set[str]:
        phon = proj.phonetic(query)
        return self.by_phonetic.get(phon, set()) if phon else set()


def build_variation_index(variant_table, backbone) -> "CandidateIndex":
    """Build a per-branch candidate index from a VariantTable, enriched with the
    backbone's stone_type and allowed colours for blocking (section 5A.1). When
    the backbone lacks the variety, the type is recovered from the variant Key and
    the colour from a colour word in the variant name, so blocking still works for
    the many variants that have no backbone record (this prevents cross-colour
    fuzzy matches like White -> the Cream variety).
    """
    from stone_pipeline.adapters.tokens import COLOR_TOKENS

    color_set = {c.casefold() for c in COLOR_TOKENS}

    def colors_from_name(name: str) -> set[str]:
        return {tok for tok in name.split() if tok.casefold() in color_set}

    index = CandidateIndex()
    for variant in variant_table.by_id.values():
        # disambiguate same-name varieties by the variant's OWN Key type (authoritative), not an
        # arbitrary first same-name backbone variety -- which (with the shared slab backbone used for
        # every branch) could carry a different stone type and set the wrong block_type.
        key_type = _type_from_key(variant.key)
        variety = backbone.lookup(variant.name, stone_type=key_type)
        block_type = (variety.stone_type if variety else "") or key_type
        block_colors = set(variety.colors) if variety and variety.colors else colors_from_name(variant.name)
        surfaces = set(variant.aliases)
        index.add(
            cid=variant.variation_id,
            canonical=variant.name,
            surfaces=list(surfaces),
            block_type=block_type,
            block_colors=block_colors,
        )
    return index


def _type_from_key(key: str) -> str:
    """variants Key looks like 'slab_marble_arabescato_<uuid>'; the token after
    the branch is the stone type."""
    parts = (key or "").split("_")
    return parts[1] if len(parts) >= 2 else ""
