"""The ONE rule for a learned guess (P2-10 C2): the matcher's phonetic and fuzzy tiers, where the index could not name
the variety by a known surface. Only such a bind is proposed as a learned alias (stages.match_variation.WriteBack,
curate's alias arms); every other bind (exact, exact narrowed by name, type, colour or origin, projection, override)
is re-derived from the seed on every run, so proposing it adds nothing but stickiness: a later seed correction (an
alias moved to its right owner) would be undone by the saved pick. The learned-alias migration judges the old
write-back's records by the same rule."""

from __future__ import annotations

# The engine tiers that GUESS (matching/engine.py: "phonetic" tier 4, "fuzzy" / "fuzzy_origin" tiers 5-6).
LEARNED_TIERS = ("phonetic", "fuzzy")
# The prefixes the matcher stage puts in front of an engine method (stages/match_variation.py:
# _match_identity "clean_variety_", the operator-type retry "operator_type_"); the tier is what follows.
_METHOD_WRAPPERS = ("clean_variety_", "operator_type_")


def is_learned(method: str) -> bool:
    """True when `method` is a guess worth keeping as an alias: its engine tier is phonetic or fuzzy. The ONE
    rule: curate's alias proposals and leaf-suggestion verdicts read it too, so they never disagree."""
    method = (method or "").strip()
    for wrapper in _METHOD_WRAPPERS:
        if method.startswith(wrapper):
            method = method[len(wrapper):]
            break
    return method.startswith(LEARNED_TIERS)
