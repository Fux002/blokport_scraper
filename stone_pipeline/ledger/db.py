"""Data-access layer for the sync ledger (SYNC_LEDGER_DESIGN.md sections 3, 4).

Thin and portable on purpose: the substrate is one swap (design section 12). Phase 1
runs SQLite on the task's LOCAL disk (never EFS, M4); the same SQL lifts to RDS
Postgres later. The only place that knows it is SQLite is `_connect`.

The DAL does three jobs and nothing clever:
  1. Open a per-env ledger, apply the schema, and assert the env guard (design
     section 2): a dev process can never write a prod ledger or vice versa.
  2. Upsert rows by primary key (idempotent, so a re-run never duplicates).
  3. Provide the deterministic hashing the rest of the ledger keys on, reusing the
     pipeline's own helpers (`core.ids`) so a ledger hash matches a pipeline hash.

Sync-state semantics (new -> pending, changed -> dirty, unchanged -> untouched) and
the per-entity desired-state mapping live one layer up, in the populate step, not
here. This file stays a store.

No em dashes (design principle 2).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from stone_pipeline.core import ids as _ids

SCHEMA_VERSION = 6   # v6: product.collection_country_code/collection_city -- supplier collection location
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class LedgerError(Exception):
    """Any ledger-level failure."""


class LedgerEnvMismatch(LedgerError):
    """The ledger belongs to a different environment than the caller (design 2)."""


def now_iso() -> str:
    """UTC ISO8601 timestamp, the one timestamp format the ledger stores."""
    return datetime.now(timezone.utc).isoformat()


def payload_hash(parts: Sequence[Any]) -> str:
    """The dirty-vs-synced hash over an entity's ordered payload fields (design M3).

    Reuses `core.ids.row_fingerprint` so a value hashed here matches the same value
    hashed anywhere else in the pipeline. The caller passes the exact ordered field
    set documented per table in schema.sql; order matters and is part of the contract.
    """
    return _ids.row_fingerprint([_canon(p) for p in parts])


def combo_key(category: str, type_: str, variation_key: str,
              color: str, finish: str, quality: str) -> str:
    """Deterministic combination key over the full priceable tuple (design C1).

    Category and type ARE dimensions, so they are in the key. Same inputs always
    give the same key, so a combination is addressable and idempotent.
    """
    return "combo_" + _ids.row_fingerprint(
        [_canon(category), _canon(type_), _canon(variation_key),
         _canon(color), _canon(finish), _canon(quality)]
    )


def _canon(value: Any) -> str:
    """Stable string form for hashing: None becomes empty, everything else str()."""
    return "" if value is None else str(value)


class Ledger:
    """A per-environment sync ledger, opened against one SQLite file.

    Use as a context manager so the connection always closes and the final commit
    is not forgotten:

        with Ledger.open(path, env="development") as ledger:
            ledger.upsert("variation", {...}, pk=("key",))
    """

    def __init__(self, conn: sqlite3.Connection, env: str):
        self.conn = conn
        self.env = env

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, env: str,
             backend_id_fingerprint: str | None = None) -> "Ledger":
        """Open (creating if absent) the ledger at `path`, bound to `env`.

        Applies the schema if the file is new, then asserts the env guard: if the
        file already belongs to a different env, refuse rather than corrupt it.
        """
        if env not in ("development", "production"):
            raise LedgerError(f"unknown env {env!r}; expected development or production")
        conn = cls._connect(path)
        ledger = cls(conn, env)
        ledger._apply_schema()
        ledger._bind_env(env, backend_id_fingerprint)
        return ledger

    @staticmethod
    def _connect(path: str | Path) -> sqlite3.Connection:
        """The one SQLite-specific spot: the connect + PRAGMAs below are the substrate seam. A Postgres
        migration adds its branch HERE (see MIGRATION_TO_POSTGRES.md). require_sqlite fails loud if a
        not-yet-wired dialect is configured, so the seam is explicit and a misconfig surfaces here."""
        from stone_pipeline.core.dbdialect import require_sqlite
        require_sqlite("ledger")
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers run concurrently with the single writer, so a pull-serve (which LEASES rows,
        # i.e. writes) no longer blocks a concurrent ack/status and vice versa. REQUIRES local disk, not
        # NFS/EFS (WAL's shared-memory index is unreliable over a network FS) -- the ledger is on the
        # task's local volume (design section 12 / M4), snapshotted to S3 for persistence.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")   # safe under WAL; far fewer fsyncs than FULL
        # two processes share this file (sync server + config server). Wait for a lock instead of
        # erroring 'database is locked' immediately, so an ack and a reset serialize rather than fail.
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _apply_schema(self) -> None:
        # Apply the DDL once per database, not on every open (the sync server opens a
        # connection per request). PRAGMA user_version makes a reopen a no-op; the
        # schema is all CREATE ... IF NOT EXISTS, so a one-time apply is sufficient.
        if self.conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()   # add columns to tables created before a field existed (CREATE IF NOT
                          # EXISTS never alters an existing table). Keeps ledger_meta in step.
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute("UPDATE ledger_meta SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION,))
        self.conn.commit()

    def _migrate(self) -> None:
        """Idempotent ALTERs for a ledger that predates a column (v1 -> v2 added the sync dead-letter
        fields; v2 -> v3 added inventory.reason, the delist marker; v3 -> v4 added removed.kind, the
        product|variation discriminator -- the `removed` table itself is a CREATE IF NOT EXISTS in
        schema.sql, applied by the re-run above). Fresh ledgers already have these from schema.sql, so
        every branch here is a no-op on them. `retiring` needed no migration (already in the CHECK)."""
        for table in ("variation", "product"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "sync_attempts" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_attempts INTEGER NOT NULL DEFAULT 0")
            if "sync_error" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_error TEXT")
            if "abandoned_at" not in cols:   # v5: a dead-letter the operator dropped (terminal, but auditable)
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN abandoned_at TEXT")
        inv_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(inventory)")}
        if "reason" not in inv_cols:   # why qty is 0: 'delisted' (admin take-offline) vs null (out of stock)
            self.conn.execute("ALTER TABLE inventory ADD COLUMN reason TEXT")
        prod_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(product)")}
        if "collection_country_code" not in prod_cols:   # v6: supplier collection location (independent of origin)
            self.conn.execute("ALTER TABLE product ADD COLUMN collection_country_code TEXT")
        if "collection_city" not in prod_cols:
            self.conn.execute("ALTER TABLE product ADD COLUMN collection_city TEXT")
        removed_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(removed)")}
        if "kind" not in removed_cols:   # v4: existing tombstones are all products (that was the only lane)
            self.conn.execute("ALTER TABLE removed ADD COLUMN kind TEXT NOT NULL DEFAULT 'product'")
        if "abandoned_at" not in removed_cols:   # v5: a dead tombstone the operator dropped
            self.conn.execute("ALTER TABLE removed ADD COLUMN abandoned_at TEXT")
        # covering index for the serve order (products before variations, E1). Created HERE, not in
        # schema.sql, because schema.sql runs BEFORE this ALTER on an old ledger, where an index on the
        # not-yet-added `kind` column would fail. Idempotent, so fresh ledgers pick it up here too.
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_removed_serve_kind "
                          "ON removed (state, kind, created_at, external_id)")

    def _bind_env(self, env: str, fingerprint: str | None) -> None:
        """Initialize or assert the single `ledger_meta` row (the env guard)."""
        row = self.conn.execute(
            "SELECT env, backend_id_fingerprint, schema_version FROM ledger_meta WHERE id = 1"
        ).fetchone()
        now = now_iso()
        if row is None:
            self.conn.execute(
                "INSERT INTO ledger_meta (id, env, backend_id_fingerprint, schema_version, "
                "created_at, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
                (env, fingerprint, SCHEMA_VERSION, now, now),
            )
            self.conn.commit()
            return
        if row["env"] != env:
            raise LedgerEnvMismatch(
                f"ledger belongs to env {row['env']!r}, refusing to open it as {env!r}"
            )
        if row["schema_version"] != SCHEMA_VERSION:
            raise LedgerError(
                f"ledger schema_version {row['schema_version']} != code {SCHEMA_VERSION}; "
                "a migration is needed"
            )
        if fingerprint is not None:
            stored = row["backend_id_fingerprint"]
            if stored is None:
                # first-time bind (a ledger created before a fingerprint was passed): record it, don't reject.
                self.conn.execute(
                    "UPDATE ledger_meta SET backend_id_fingerprint = ?, updated_at = ? WHERE id = 1",
                    (fingerprint, now_iso()),
                )
                self.conn.commit()
            elif stored != fingerprint:
                # This ledger belongs to a DIFFERENT brand/backend -- a wrong-brand snapshot restored onto
                # this task, or a re-seed. FAIL LOUD instead of silently accepting + clobbering it (a genuine
                # re-seed is an explicit operator action). This is the ledger's brand-identity guard.
                raise LedgerEnvMismatch(
                    f"ledger backend fingerprint {stored!r} != this deployment's {fingerprint!r} -- refusing "
                    "to open. It belongs to a different brand/backend (a wrong-brand snapshot restore or a "
                    "re-seed). Restore the correct ledger, or re-seed explicitly.")

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()

    # -- writes ------------------------------------------------------------

    def upsert(self, table: str, row: dict[str, Any], pk: Sequence[str],
               keep_on_update: Sequence[str] = ("created_at", "first_seen")) -> None:
        """Insert `row`, or update it in place on a primary-key conflict.

        Idempotent by construction: re-upserting identical data is a no-op write.
        Columns in `keep_on_update` (the immutable timestamps) are written only on
        insert and preserved on update, so `created_at`/`first_seen` never move.
        """
        cols = list(row)
        placeholders = ", ".join("?" for _ in cols)
        conflict = ", ".join(pk)
        updatable = [c for c in cols if c not in pk and c not in keep_on_update]
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in updatable)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
            if set_clause
            else
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO NOTHING"
        )
        self.conn.execute(sql, [row[c] for c in cols])

    def set_state(self, table: str, pk_col: str, pk_value: Any, state: str) -> None:
        """Move one entity to a new sync state, stamping updated_at."""
        self.conn.execute(
            f"UPDATE {table} SET state = ?, updated_at = ? WHERE {pk_col} = ?",
            (state, now_iso(), pk_value),
        )

    # -- reads -------------------------------------------------------------

    def get(self, table: str, pk_col: str, pk_value: Any) -> sqlite3.Row | None:
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {pk_col} = ?", (pk_value,)
        ).fetchone()

    def iter_state(self, table: str, state: str) -> Iterator[sqlite3.Row]:
        """Rows of `table` in a given state, in the stable serve order (design L2)."""
        cur = self.conn.execute(
            f"SELECT * FROM {table} WHERE state = ? ORDER BY created_at, rowid", (state,)
        )
        yield from cur

    def counts(self, table: str) -> dict[str, int]:
        """state -> count for one table, for GET /sync/status and the run summary."""
        cur = self.conn.execute(f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state")
        return {r["state"]: r["n"] for r in cur}

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Escape hatch for the populate/render layers; the DAL stays thin."""
        return self.conn.execute(sql, tuple(params))
