"""Deterministic name-identity resolver: decides whether a scraped variety name is an ALIAS (another
spelling) of an existing variety or a genuinely NEW stone.

Variety identity is the NAME within a type. TYPE and COLOUR are NOT identity evidence here -- they only
BLOCK upstream (the engine never matches across a stone type, and colour breaks a same-type tie). An earlier
trained model let type + colour DOMINATE the alias decision, so two unrelated same-type/same-colour stones
(e.g. 'Azul Palomino' and 'Capolavoro' quartzites) were auto-merged. This resolver scores NAME evidence ONLY:

  - a near-identical spelling (de-spaced char similarity >= the alias floor)     -> alias  (typo net:
    'Ice Burg' ~ 'Iceberg', 'Bianco Carara' ~ 'Bianco Carrara')
  - a difference that is ONLY generic descriptor words                           -> alias  ('Bardiglio
    Nuvolato' ~ 'Bardiglio Nuvolato Marble')
  - some, but not confident, name similarity                                     -> review (a human decides
    same-stone vs distinct sibling: 'Cristallo Divine' vs 'Cristallo Bianco')
  - no name evidence                                                             -> mint   (a new variety)

A confident 'alias' never crosses a stone-type boundary (that stays a review). Deterministic and idempotent:
no training, no probability model, no per-run randomness -- the same names always decide the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import distance

from stone_pipeline.core import logfmt
from stone_pipeline.core.text import match_key

log = logfmt.get_logger("alias_resolver")

# The generic-descriptor words (that, as the ONLY difference between two names, signal an alias not a
# distinct variety) now live in the active domain pack -- `active_pack().generic_descriptors` -- so a
# non-stone pack supplies its own. Read lazily in `decide_against` (see AliasResolver._generic_set).

# De-spaced char similarity above which two names are the SAME variety (a typo/spelling variant); and above
# which an uncertain near-match is REVIEWED rather than minted. A distinct sibling ('Cristallo Divine' vs
# 'Cristallo Bianco') shares a prefix but stays below the alias floor, so it is never auto-merged.
_CHAR_ALIAS_FLOOR = 0.94
_CHAR_REVIEW_FLOOR = 0.88


def _norm(s: str) -> str:
    # the shared canonical key: ascii-fold accents too, so 'Porriño'/'Porrino' don't look different.
    return match_key(s)


@dataclass
class Decision:
    verdict: str   # "alias" | "mint" | "review"
    prob: float    # the de-spaced char similarity that drove the verdict (0..1), for the review card


@dataclass
class AliasResolver:
    """Stateless deterministic decider. Kept as a class (built by `from_backbones`) so the curate call site
    is unchanged; it holds no trained parameters."""

    # the pack's generic-descriptor words; empty means "read the active pack lazily" (see _generic_set)
    generic: frozenset = frozenset()

    def _generic_set(self) -> frozenset:
        if self.generic:
            return self.generic
        from stone_pipeline.config.domain import active_pack   # lazy: avoid an import cycle at module load
        return active_pack().generic_descriptors

    def decide(self, a: str, ta: str, ca, b: str, tb: str, cb) -> Decision:
        return self.decide_against(a, ta, ca, [b], tb, cb)

    def decide_against(self, a: str, ta: str, ca, surfaces: list[str], tb: str, cb) -> Decision:
        """Score the new name `a` against ALL surfaces of the candidate variety (its canonical name AND its
        known aliases) and take the strongest NAME evidence. `ca`/`cb` (colours) are accepted for interface
        compatibility but deliberately NOT used -- colour is not identity. `ta`/`tb` gate a cross-type alias
        down to review, nothing more."""
        generic = self._generic_set()
        da = _norm(a).replace(" ", "")
        atoks = set(_norm(a).split())
        best_char = 0.0
        only_generic = False
        meaningful_diff = False
        for s in surfaces:
            if not s:
                continue
            ds = _norm(s).replace(" ", "")
            best_char = max(best_char, distance.JaroWinkler.normalized_similarity(da, ds))
            btoks = set(_norm(s).split())
            shared = atoks & btoks
            extra = (atoks | btoks) - shared
            if shared and extra:
                # a shared varietal core with the ONLY differing tokens being generic descriptors is the same
                # variety ('Bardiglio Nuvolato' ~ '... Marble'); a shared core with a MEANINGFUL differing
                # word is a DISTINCT sibling ('Cristallo Divine' vs 'Cristallo Bianco'), never an alias.
                if extra <= generic:
                    only_generic = True
                else:
                    meaningful_diff = True
        if only_generic or best_char >= _CHAR_ALIAS_FLOOR:
            verdict = "alias"                       # generic-only difference, or a near-identical spelling
        elif meaningful_diff:
            verdict = "mint"                        # shares a core but a real word differs -> distinct variety
        elif best_char >= _CHAR_REVIEW_FLOOR:
            verdict = "review"                      # an ambiguous near-match with no clear token signal
        else:
            verdict = "mint"                        # no name evidence -> a genuinely new variety
        # Type gate: never AUTO-confirm an alias across a stone-type boundary (a Marble 'Tiger Black' and a
        # Granite 'Tiger Black' are DIFFERENT varieties). Only the confident 'alias' is downgraded; a missing
        # type on either side is no signal, so it does not gate.
        if verdict == "alias" and ta and tb and _norm(ta) != _norm(tb):
            verdict = "review"
        return Decision(verdict, round(best_char, 3))


def from_backbones():
    """Build the nearest-variety lookup from every category's backbone and return
    (resolver, name_norm -> (stone_type, colors, aliases)) -- the lookup the mint decision uses for the
    nearest variety's type + aliases. The resolver is deterministic (no labelled-data requirement), so it is
    ALWAYS available (never (None, {}))."""
    from stone_pipeline.config.settings import CATEGORIES
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_backbone

    meta: dict[str, tuple] = {}
    seen: set = set()
    for c in CATEGORIES:
        path = c.backbone_path
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        for vlist in load_backbone(path).by_norm_name.values():
            for v in vlist:
                meta[proj.norm(v.variant)] = (v.stone_type, v.colors, v.aliases)
    log.info("alias resolver ready (deterministic name-identity)",
             extra={"extra_fields": {"varieties": len(meta),
                                     "alias_floor": _CHAR_ALIAS_FLOOR, "review_floor": _CHAR_REVIEW_FLOOR}})
    return AliasResolver(), meta
