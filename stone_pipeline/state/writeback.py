"""Outbound write-back (section 8.4), now PROPOSALS (P2-10 C2).

A confirmed phonetic or fuzzy variation match proposes the scraped spelling as a learned alias of the variety it bound:
a row in config.db `learned_alias`, keyed by the variety's identity Key, with the match METHOD and the vendor as
provenance. A proposal is not a matcher input: only an operator decision makes it active (decisions_store.learned).

ONLY a learned GUESS is proposed (is_learned): a phonetic or fuzzy match, the tiers where the index could not name
the variety by a known surface. Every other bind (exact, exact narrowed by name, type, colour or origin, projection,
override) is re-derived from the seed on every run, so proposing it adds nothing but stickiness: a later seed
correction (an alias moved to its right owner) would be undone by the saved pick.

The old state/alias_writeback.csv, its reader and its writer belong to the one-time move into learned_alias
(aliases.migrate_learned) and the phase 1 store migration; nothing here reads or writes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass
class WriteBack:
    """Accumulates the run's learned guesses; flushed at the end as proposals."""

    aliases: list[tuple] = field(default_factory=list)  # (identity Key, alias, method, source, score)
    seen_aliases: set[tuple[str, str]] = field(default_factory=set)

    def add_alias(self, identity_key: str, alias: str, method: str = "", source: str = "",
                  score: float | None = None) -> None:
        alias = (alias or "").strip()
        if not identity_key or not alias:
            return
        key = (identity_key, alias.casefold())
        if key in self.seen_aliases:
            return
        self.seen_aliases.add(key)
        self.aliases.append((identity_key, alias, (method or "").strip(), (source or "").strip(), score))

    def flush(self) -> int:
        """Propose this run's learned guesses (decisions_store.propose_learned_aliases: a Key and norm already holding
        a row is left as it is). Returns the number of new proposals."""
        from stone_pipeline.config import decisions_store
        return decisions_store.propose_learned_aliases(
            {"identity_key": key, "value": alias, "method": method, "source": source, "score": score}
            for key, alias, method, source, score in self.aliases)

