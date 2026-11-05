"""Outbound write-back (section 8.4).

A confirmed phonetic or fuzzy variation match appends the scraped spelling
to that variation's alias list. This is what makes the system more consistent every
run instead of re-reviewing the same names forever. Write-backs are recorded in the
manifest. (Origin is NOT written back automatically -- origin_map.csv is hand-maintained
in catalog_source/; the origin review flag is the worklist.)

Rather than mutate the live backend exports (which can be re-pulled), confirmed
aliases are appended to state/alias_writeback.csv. The variation index loader
reads this file in addition to the exports, so the next run resolves the spelling
as an exact alias hit for free.

Each row records the MATCH METHOD that learned it, so a learned alias is auditable and
distinguishable from a hand-curated export alias, and can be reversed with `forget_alias` if a
wrong high-scoring fuzzy guess was persisted.

ONLY a learned GUESS is kept (is_learned): a phonetic or fuzzy match, the tiers where the index
could not name the variety by a known surface. Every other bind (exact, exact narrowed by name,
type, colour or origin, projection, override) is re-derived from the seed on every run, so
persisting it adds nothing but stickiness: a later seed correction (an alias moved to its right
owner) would be undone by the saved pick. Rows recorded before this rule are not applied and are
dropped from the file on the next flush.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.core.csvio import atomic_write

log = logfmt.get_logger("writeback")

ALIAS_WRITEBACK = "alias_writeback.csv"
_FIELDS = ["variation_id", "alias", "method"]

# The engine tiers that GUESS (matching/engine.py: "phonetic" tier 4, "fuzzy" / "fuzzy_origin" tiers 5-6).
LEARNED_TIERS = ("phonetic", "fuzzy")
# The prefixes the matcher stage puts in front of an engine method (stages/match_variation.py:
# _match_identity "clean_variety_", the operator-type retry "operator_type_"); the tier is what follows.
_METHOD_WRAPPERS = ("clean_variety_", "operator_type_")


def is_learned(method: str) -> bool:
    """True when `method` is a guess worth keeping as an alias: its engine tier is phonetic or fuzzy."""
    method = (method or "").strip()
    for wrapper in _METHOD_WRAPPERS:
        if method.startswith(wrapper):
            method = method[len(wrapper):]
            break
    return method.startswith(LEARNED_TIERS)


def _writeback_path(name: str) -> Path:
    return SETTINGS.paths.state_dir / name


@dataclass
class WriteBack:
    """Accumulates confirmed learnings during a run; flushed at the end."""

    aliases: list[tuple[str, str, str]] = field(default_factory=list)  # (variation_id, alias, method)
    seen_aliases: set[tuple[str, str]] = field(default_factory=set)

    def add_alias(self, variation_id: str, alias: str, method: str = "") -> None:
        alias = (alias or "").strip()
        if not variation_id or not alias:
            return
        key = (variation_id, alias.casefold())
        if key in self.seen_aliases:
            return
        self.seen_aliases.add(key)
        self.aliases.append((variation_id, alias, (method or "").strip()))

    def flush(self, path: Path | None = None) -> int:
        """Persist newly-learned aliases, skipping ones already recorded, and drop recorded rows that are
        not learned guesses (is_learned). Returns the count of new aliases written. Idempotent: with nothing
        new and nothing to drop it does not touch the file, so a re-run is a no-op. Otherwise the whole file
        is rewritten under the current schema, which is deterministic -- same runs produce the same file."""
        path = Path(path or _writeback_path(ALIAS_WRITEBACK))
        recorded = load_alias_writeback_records(path)
        kept = [record for record in recorded if is_learned(record[2])]
        kept_keys = {(vid, alias.casefold()) for vid, alias, _ in kept}
        new = [(vid, alias, method) for vid, alias, method in self.aliases
               if is_learned(method) and (vid, alias.casefold()) not in kept_keys]
        dropped = len(recorded) - len(kept)
        if not new and not dropped:
            return 0
        if dropped:
            log.info("alias write-back: dropped rows that are not learned guesses",
                     extra={"extra_fields": {"dropped": dropped, "kept": len(kept)}})
        _write_records(path, kept + new)
        return len(new)


def _write_records(path: Path, records: list[tuple[str, str, str]]) -> None:
    # Atomic temp-file + os.replace: the whole-file rewrite must never leave a truncated file (which would
    # lose ALL previously-learned aliases, not just the tail), and overlapping produce/build writers must not
    # tear each other's output. Last-writer-wins is fine here; a torn file is not.
    def _write(handle) -> None:
        writer = csv.writer(handle)
        writer.writerow(_FIELDS)
        for vid, alias, method in records:
            writer.writerow([vid, alias, method])
    atomic_write(path, _write)


def load_alias_writeback_records(path: Path | None = None) -> list[tuple[str, str, str]]:
    """Full learned-alias records (variation_id, alias, method) for audit/undo. Tolerates a legacy
    two-column file (method reads as "")."""
    path = Path(path or _writeback_path(ALIAS_WRITEBACK))
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


def load_alias_writeback(path: Path | None = None) -> list[tuple[str, str]]:
    """The (variation_id, alias) pairs the matcher applies -- the stable contract the index loader reads:
    the learned guesses only, so a row recorded before the rule never binds even before the file is healed."""
    return [(vid, alias) for vid, alias, method in load_alias_writeback_records(path) if is_learned(method)]


def forget_alias(variation_id: str, alias: str, path: Path | None = None) -> bool:
    """Remove one learned alias (the undo path for a wrong fuzzy/phonetic guess that was persisted as an
    exact alias). Rewrites the file without that (variation_id, alias) row. Returns True if a row was
    removed, False if it was not present. Case-insensitive on the alias, matching how it is keyed."""
    path = Path(path or _writeback_path(ALIAS_WRITEBACK))
    records = load_alias_writeback_records(path)
    if not records:
        return False
    target = (variation_id, (alias or "").strip().casefold())
    kept = [r for r in records if (r[0], r[1].casefold()) != target]
    if len(kept) == len(records):
        return False
    _write_records(path, kept)
    return True
