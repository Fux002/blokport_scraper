"""Outbound write-back (section 8.4), now PROPOSALS (P2-10 C2).

A confirmed phonetic or fuzzy variation match proposes the scraped spelling as a learned alias of the variety it bound:
a row in config.db `learned_alias`, keyed by the variety's identity Key, with the match METHOD and the vendor as
provenance. A proposal is not a matcher input: only an operator decision makes it active (decisions_store.learned).

ONLY a learned GUESS is proposed (is_learned): a phonetic or fuzzy match, the tiers where the index could not name
the variety by a known surface. Every other bind (exact, exact narrowed by name, type, colour or origin, projection,
override) is re-derived from the seed on every run, so proposing it adds nothing but stickiness: a later seed
correction (an alias moved to its right owner) would be undone by the saved pick.

The old state/alias_writeback.csv is read only by the phase 1 store migration and by the one-time move into
learned_alias (aliases.migrate_learned); nothing writes it any more.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.core.csvio import atomic_write

ALIAS_WRITEBACK = "alias_writeback.csv"
_FIELDS = ["variation_id", "alias", "method"]

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


def _write_records(path: Path, records: list[tuple[str, str, str]]) -> None:
    # The phase 1 store migration's writer (phase1/migrate_run.py). Atomic temp-file + os.replace: a whole-file
    # rewrite must never leave a truncated file.
    def _write(handle) -> None:
        writer = csv.writer(handle)
        writer.writerow(_FIELDS)
        for vid, alias, method in records:
            writer.writerow([vid, alias, method])
    atomic_write(path, _write)


def load_alias_writeback_records(path: Path) -> list[tuple[str, str, str]]:
    """The old file's records (variation_id, alias, method); [] when it is absent. Tolerates a legacy two-column file
    (method reads as "")."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[tuple[str, str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            vid = (record.get("variation_id") or "").strip()
            alias = (record.get("alias") or "").strip()
            method = (record.get("method") or "").strip()
            if vid and alias:
                out.append((vid, alias, method))
    return out
