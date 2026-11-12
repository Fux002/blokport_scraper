"""Learned aliases (P2-10 C2): the spellings the pipeline GUESSED for a variety, in config.db `learned_alias`, keyed by
the variety's identity Key and norm = core.text.match_key(value).

The lifecycle is one rule: the matcher's guesses and curate's alias arms PROPOSE; an operator decision makes a row
ACTIVE or REJECTED; only an active row is a matcher input. A (Key, norm) holds one row, so a proposal never
overwrites a decision and a rejected norm is never proposed again. No reset clears the table.
"""

from __future__ import annotations

from contextlib import closing
from typing import Iterable

from stone_pipeline.config import store
from stone_pipeline.core.text import match_key

from ._common import InvalidDecision, _now

_COLUMNS = ("identity_key", "value", "norm", "method", "source", "status", "first_seen", "decided_by", "decided_at")
_DECISIONS = ("active", "rejected")


def active_learned_aliases() -> list[dict]:
    """Every ACTIVE learned alias, one dict per row with all of its columns, sorted by (identity_key, norm). Read-only:
    an absent store reads as empty and is not created."""
    with closing(store.read_store()) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM learned_alias WHERE status = 'active' ORDER BY identity_key, norm")]


def _row(proposal: dict) -> tuple:
    key, value = (proposal.get("identity_key") or "").strip(), (proposal.get("value") or "").strip()
    norm = match_key(value)
    if not key or not norm:
        raise InvalidDecision(f"a learned alias needs an identity Key and a value with a key: {proposal!r}")
    return (key, value, norm, (proposal.get("method") or "").strip(), (proposal.get("source") or "").strip(),
            proposal.get("score"))


def propose_learned_aliases(proposals: Iterable[dict]) -> int:
    """Record each {identity_key, value, method, source, score} as a PROPOSED learned alias (score: the proposing
    arm's own score on 0..100, or None). A (Key, norm) that already holds
    a row (proposed, active or rejected) is left as it is, so a decision is never undone and a rejected norm is never
    proposed again. Returns the number of new proposals."""
    rows = [_row(p) for p in proposals]
    if not rows:
        return 0
    if not store.config_db_path().exists():
        # The produce writes here on its own (no operator act), so on a host with no config.db yet (a laptop run,
        # CI's certify) this write would create the store EMPTY, and load_sources would then read that store and find
        # no sources. Seed it from sources.yaml first, exactly as the config server does at boot.
        store.seed_from_yaml()
    now = _now()
    with closing(store.open_store()) as conn, conn:
        return sum(conn.execute(
            "INSERT OR IGNORE INTO learned_alias (identity_key, value, norm, method, source, score, status, "
            "first_seen) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)", (*row, now)).rowcount for row in rows)


def decide_learned_alias(identity_key: str, value: str, status: str, decided_by: str) -> bool:
    """The decision on one learned alias, found by its Key and the norm of `value`: 'active' (a matcher input) or
    'rejected' (never proposed again). False when no such row exists (nothing changes)."""
    if status not in _DECISIONS:
        raise InvalidDecision(f"a learned alias is decided 'active' or 'rejected', not {status!r}")
    with closing(store.open_store()) as conn, conn:
        return conn.execute("UPDATE learned_alias SET status = ?, decided_by = ?, decided_at = ? "
                            "WHERE identity_key = ? AND norm = ?",
                            (status, decided_by, _now(), identity_key, match_key(value))).rowcount == 1


def learned_migration_done(name: str) -> bool:
    """True when config.db records the one-time migration `name` (read-only)."""
    with closing(store.read_store()) as conn:
        return conn.execute("SELECT 1 FROM migration WHERE name = ?", (name,)).fetchone() is not None


def record_learned_migration(name: str, rows: Iterable[dict]) -> int:
    """The one-time migration's write, in ONE transaction: each {identity_key, value, method, source} becomes ACTIVE,
    decided_by 'migration' and with no score (the old file kept none), and the marker `name` is recorded, so a failure
    leaves neither. A (Key, norm) already proposed is activated; one already decided keeps its decision. Returns the
    rows written."""
    now = _now()
    written = 0
    with closing(store.open_store()) as conn, conn:
        for row in map(_row, rows):
            written += conn.execute(
                "INSERT INTO learned_alias (identity_key, value, norm, method, source, score, status, first_seen, "
                "decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 'migration', ?) "
                "ON CONFLICT (identity_key, norm) DO UPDATE SET status = 'active', decided_by = 'migration', "
                "decided_at = excluded.decided_at WHERE learned_alias.status = 'proposed'", (*row, now, now)).rowcount
        conn.execute("INSERT INTO migration (name, applied_at) VALUES (?, ?)", (name, now))
    return written
