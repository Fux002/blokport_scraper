"""The sync ledger: the scraper-side system of record for what should be in Medusa.

Phase 1 is additive and parallel. Nothing here changes the existing CSV flow; the
ledger is populated alongside it and proven to reproduce the `to_upload` CSVs
byte-for-byte before any CSV is retired (SYNC_LEDGER_DESIGN.md section 13).

The data model is `schema.sql` (design section 4). `db.py` is the thin, portable
data-access layer over it (SQLite for Phase 1, liftable to RDS Postgres).
"""

from __future__ import annotations

from stone_pipeline.ledger.db import (
    Ledger,
    LedgerEnvMismatch,
    LedgerError,
    SCHEMA_VERSION,
    combo_key,
    payload_hash,
)

__all__ = [
    "Ledger",
    "LedgerError",
    "LedgerEnvMismatch",
    "SCHEMA_VERSION",
    "combo_key",
    "payload_hash",
]
