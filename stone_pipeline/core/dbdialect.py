"""The database dialect knob (Phase 4: RDS-readiness).

BLOKPORT_DB_DIALECT selects the SQL substrate; only 'sqlite' is wired today. Postgres is a planned migration
-- the SQL itself is already portable (ON CONFLICT, CHECK, FK, JSON-as-TEXT, hashlib ids). What remains is a
connect factory + gating the SQLite-only PRAGMAs/snapshot lane. See MIGRATION_TO_POSTGRES.md.

This module is the ONE place the dialect decision lives, so the migration is a config change plus two small
connect branches, not a hunt through the code. require_sqlite() is called wherever a SQLite-only connection
is opened, so a misconfigured dialect fails LOUD at that boundary instead of as a cryptic driver error.
"""

from __future__ import annotations

from stone_pipeline.core import env

SQLITE = "sqlite"
POSTGRES = "postgres"


def dialect() -> str:
    """The configured DB dialect (BLOKPORT_DB_DIALECT), default 'sqlite'."""
    return env.getenv("BLOKPORT_DB_DIALECT", SQLITE).strip().lower()


def require_sqlite(component: str) -> None:
    """Fail loud if a non-sqlite dialect is configured: Postgres is not wired yet. This marks the single
    slot-in point for each DB substrate, so an operator who sets BLOKPORT_DB_DIALECT=postgres before the
    migration is done gets a clear, actionable error here rather than a driver import failure downstream."""
    current = dialect()
    if current != SQLITE:
        raise NotImplementedError(
            f"{component}: BLOKPORT_DB_DIALECT={current!r} is not wired yet (only {SQLITE!r} is). Postgres is "
            f"a planned migration -- add its driver + a connect branch HERE and gate the SQLite PRAGMAs/"
            f"snapshot. See MIGRATION_TO_POSTGRES.md.")
