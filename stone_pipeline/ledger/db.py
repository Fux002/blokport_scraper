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
from typing import Any, Iterable, Sequence

from stone_pipeline.core import ids as _ids

SCHEMA_VERSION = 16  # v15: phase 1 (PHASE-1-SPEC rev 8): one variation row per (type, name) under the new Key,
#                      variation_category_image, and branch/image_url/image_sha256/image_model/volume and the
#                      combination table gone. No in-place migration below v15: the phase 1 store
#                      migration (phase1.migrate_stores) writes the stores; an older ledger is refused (_apply_schema).
#                      v16: product.photo_unpublished -- the standardised files a product links that are not
#                           published (never served while any); null on every existing row until its next produce.
PHASE1_VERSION = 15  # the first phase 1 ledger: below it only the store migration opens a ledger; from it, in place
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

def on_sale_sql(alias: str) -> str:
    """The one rule for an inventory row (aliased `alias`) on sale: stock above 0, or made to order (no count).
    Its negation is stock 0, the reversible delist (qty is never negative, and NULL only when made to order)."""
    return f"({alias}.qty > 0 OR {alias}.made_to_order = 1)"


def in_medusa_sql(alias: str) -> str:
    """The one rule for a product (aliased `alias`) Medusa holds: Medusa acked its id (a non-empty medusa_id), or it
    was synced once (last_synced: set by a successful ack, and by the rows the products export shows, the bootstrap
    seed and populate_discontinued's minimal rows, which Medusa holds by SKU). Whatever its sync state now: a dirty,
    leased or held product is still in Medusa. A reset clears both, so a reset product is new again."""
    return f"(({alias}.medusa_id IS NOT NULL AND {alias}.medusa_id != '') OR {alias}.last_synced IS NOT NULL)"


# The inventory rows whose count (qty, made_to_order) differs from the last synced count, or was never synced
# (last_synced_made_to_order NULL): the count half of STOCK_DELTA_SQL.
COUNT_DELTA_SQL = ("(i.last_synced_made_to_order IS NULL OR i.last_synced_made_to_order != i.made_to_order "
                   "OR i.last_synced_qty IS NOT i.qty)")
# The inventory rows whose stock differs from the last synced stock. The stock is everything the sync lane serves:
# (qty, made_to_order, reason, replaced_by_lot_code), so a reason or lot that changes at an unchanged count is a delta
# too. Over the inventory table aliased `i`; the one definition.
STOCK_DELTA_SQL = (f"({COUNT_DELTA_SQL} OR i.last_synced_reason IS NOT i.reason "
                   "OR i.last_synced_lot_code IS NOT i.replaced_by_lot_code)")
# An un-hide: the stock is on sale (on_sale_sql) while the last synced one was not (stock 0 with any reason, or never
# synced). The same rule as on_sale_sql, over the last synced columns; NULL (never synced) reads as not on sale.
UNHIDE_SQL = f"({on_sale_sql('i')} AND NOT COALESCE(i.last_synced_qty > 0 OR i.last_synced_made_to_order = 1, 0))"
# Record the current stock as synced (a successful inventory ack).
RECORD_SYNCED_STOCK_SQL = ("last_synced_qty = qty, last_synced_made_to_order = made_to_order, "
                           "last_synced_reason = reason, last_synced_lot_code = replaced_by_lot_code")
# Clear an inventory row's failure bookkeeping (the product lane's sync_attempts/sync_error/retry_after/abandoned_at):
# a success, a requeue, a reset, or a new stock (the failures counted belong to the stock that failed, not to the one
# now to serve, as new product content clears a product's).
CLEAR_STOCK_FAILURES_SQL = "sync_attempts = 0, sync_error = NULL, retry_after = NULL, abandoned_at = NULL"
# Forget the last synced stock, so the row serves again from zero (a reset).
FORGET_SYNCED_STOCK_SQL = ("last_synced_qty = NULL, last_synced_made_to_order = NULL, last_synced_reason = NULL, "
                           "last_synced_lot_code = NULL, " + CLEAR_STOCK_FAILURES_SQL)
# Re-serve a variation once with its whole payload, a re-mark (POST /sync/v1/reserve, an un-archive). Every state
# becomes 'dirty' except pending and dirty (they already serve once), retiring (being deleted, its tombstone serves
# /removed: never resurrected) and an operator-abandoned dead-letter (a terminal verdict: only new content or a global
# reset clears it); a leased (syncing) row too. What keeps an ack of a lease issued BEFORE the mark from
# syncing the row away: for a content change (populate.py) it is the new payload_hash, which that ack's echoed hash
# no longer matches; a re-mark keeps the hash, so it is the stamp reserved_at. The ack records what Medusa did but
# leaves a stamped row dirty, and only the next lease clears the stamp (sync.ready_variations), so only a lease issued
# after the mark can sync it. The one parameter is the time.
RESERVE_SQL = ("state = CASE WHEN state IN ('pending', 'dirty', 'retiring') OR abandoned_at IS NOT NULL THEN state "
               "ELSE 'dirty' END, "
               "reserved_at = COALESCE(reserved_at, ?)")


class LedgerError(Exception):
    """Any ledger-level failure."""


class LedgerEnvMismatch(LedgerError):
    """The ledger belongs to a different environment than the caller (design 2)."""


# The states each sync-state table can hold, in schema.sql's CHECK order (a test pins them to it): what
# GET /sync/v1/status reports for every table, 0 for a state with no rows, so a count is always a value.
_SYNC_STATES = ("pending", "dirty", "syncing", "synced", "needs_resync", "gap_held", "retiring")
STATES: dict[str, tuple[str, ...]] = {
    "attribute": ("pending", "dirty", "syncing", "synced", "needs_resync"),
    "variation": _SYNC_STATES,
    "product": _SYNC_STATES,
    "gap": ("open", "resolved"),
    "removed": ("pending", "dead"),
}

# Keys per IN (...) list: below SQLite's oldest bound-parameter limit (999), so one statement never exceeds it.
_IN_BATCH = 500


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
        # Apply the DDL once per database, not on every open (the sync server opens a connection per request).
        # PRAGMA user_version makes a reopen a no-op. A brand-new file (version 0) gets the schema; a ledger from
        # before phase 1 (1 to 14) is REFUSED: its rows carry old Keys, and only the store migration may turn them
        # into phase 1 rows (PHASE-1-SPEC step 5). Quietly applying the new DDL over it would serve a half-old ledger.
        # A phase 1 ledger (v15 on) gains the later columns in place (_migrate).
        from_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if from_version >= SCHEMA_VERSION:
            return
        if 0 < from_version < PHASE1_VERSION:
            raise LedgerError(f"ledger schema {from_version} predates phase 1 (v{PHASE1_VERSION}): run the store "
                              "migration (RUN_MODE=phase1-migrate), never open it in place")
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute("UPDATE ledger_meta SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION,))
        self.conn.commit()

    def _migrate(self) -> None:
        """The columns added after v15, onto a phase 1 ledger created before them (CREATE IF NOT EXISTS never alters
        an existing table). Each is a no-op on a fresh file, which schema.sql already gave them."""
        prod_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(product)")}
        if "photo_unpublished" not in prod_cols:   # v16: no backfill, the next produce of each source computes it
            self.conn.execute("ALTER TABLE product ADD COLUMN photo_unpublished TEXT")

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

    def counts(self, table: str) -> dict[str, int]:
        """state -> count for one table, for GET /sync/status and the run summary."""
        cur = self.conn.execute(f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state")
        return {r["state"]: r["n"] for r in cur}

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Escape hatch for the populate/render layers; the DAL stays thin."""
        return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        """One statement over many parameter rows (populate's icon pairs)."""
        return self.conn.executemany(sql, [tuple(r) for r in rows])


def variation_icons(ledger: Ledger, keys: Iterable[str] | None = None) -> dict[str, list[sqlite3.Row]]:
    """{Key: its `variation_category_image` rows} for `keys`, or for every variety when None. A Key with no icon is
    absent from the result."""
    sql = "SELECT key, category_handle, s3_key, etag FROM variation_category_image"
    out: dict[str, list[sqlite3.Row]] = {}
    if keys is None:
        batches: list[list[str] | None] = [None]
    else:
        wanted = list(keys)
        batches = [wanted[i:i + _IN_BATCH] for i in range(0, len(wanted), _IN_BATCH)]
    for batch in batches:
        rows = (ledger.execute(sql) if batch is None
                else ledger.execute(f"{sql} WHERE key IN ({','.join('?' * len(batch))})", batch))
        for r in rows:
            out.setdefault(r["key"], []).append(r)
    return out
