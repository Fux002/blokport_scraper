# Migrating the data layer from SQLite to Postgres (RDS)

The pipeline runs on SQLite today (the task's local disk, snapshotted to S3). The design keeps the SQL
portable so the substrate is one swap. This document is the verified map: exactly what is SQLite-specific,
what is already portable, and the recipe to add Postgres. The dialect is a single config knob
(`BLOKPORT_DB_DIALECT`, default `sqlite`) in `stone_pipeline/core/dbdialect.py`; `require_sqlite()` fails
loud at each connect until Postgres is wired, so a premature `BLOKPORT_DB_DIALECT=postgres` gives a clear
error, not a cryptic driver failure.

## What is SQLite-specific (the whole surface, verified)

There are TWO SQLite databases -- the sync ledger (per env) and the config store (`config.db`). The
SQLite-only code is confined to their connect + schema-application paths:

| Spot | File | What |
|---|---|---|
| Ledger connect + PRAGMAs | `stone_pipeline/ledger/db.py` `_connect` | `sqlite3.connect`, Row factory, `PRAGMA foreign_keys / journal_mode=WAL / synchronous=NORMAL / busy_timeout` |
| Ledger schema-version gate | `ledger/db.py` `_apply_schema` | `PRAGMA user_version` (read + set) |
| Ledger idempotent migrations | `ledger/db.py` `_migrate` | `PRAGMA table_info(<t>)` to add missing columns |
| Ledger snapshot lane | `ledger/snapshot.py` | `sqlite3.backup()` -> S3 (durability for the ephemeral local file) |
| Config-store connect + PRAGMAs | `config/store.py` `_connect` | `sqlite3.connect`, `PRAGMA journal_mode=WAL / busy_timeout / user_version` |
| Config-store migrations | `config/store.py` `_migrate` | `PRAGMA table_info(<t>)` for idempotent ALTERs |

Everything else is portable.

## What is already portable (no change needed)

- **All the SQL**: `INSERT ... ON CONFLICT (...) DO UPDATE/NOTHING` (Postgres 9.5+), `CHECK`, foreign keys,
  JSON stored as `TEXT`, and the deterministic `hashlib`/`uuid5` ids (computed in Python, not the DB).
- **The DAL** (`db.py` `upsert`/`set_state`/`get`/`iter_state`/`execute`) -- raw parameterized SQL.
- **Indexes** -- already defined for every serve predicate and for `product(source)`
  (`idx_product_serve`, `idx_product_source`, `idx_variation_serve`, `idx_combination_serve`, ...).
- **Pagination** -- `_sub_limit(limit)` on the serve lanes (`sync.py`).
- **Write batching** -- `ledger.upsert` does NOT commit per row; a loop (e.g. `record_tombstones`) runs in
  ONE transaction, so bulk writes are already efficient.
- **The two CHECK constraints** in `ledger/schema.sql` (`branch`, `category`) mirror the active domain pack
  (stone). A non-stone product pack must regenerate them from the pack -- already noted inline in the schema.

## Recipe (the migration, when the cutover is scheduled)

1. Add the driver: `psycopg[binary]` to `stone_pipeline/requirements.txt`.
2. In `core/dbdialect.py`, add a `postgres` branch and a `connect(dsn)` factory (DSN from
   `BLOKPORT_DB_DSN`); replace the `require_sqlite()` guards in the two `_connect`s with a dialect switch.
3. Gate the PRAGMAs: apply them only for `sqlite`. Postgres needs none of them
   (`foreign_keys` are always on; WAL/`synchronous`/`busy_timeout` are SQLite-only; MVCC handles the
   reader/writer concurrency WAL was giving us).
4. Schema-version gate: replace `PRAGMA user_version` with a one-row `schema_version` table (or a Postgres
   GUC). Replace `PRAGMA table_info(<t>)` with `information_schema.columns` lookups in the two `_migrate`s.
5. Snapshot lane: `sqlite3.backup()` -> rely on RDS automated backups, or `pg_dump`/a logical snapshot.
   The S3 snapshot exists because the SQLite file is on ephemeral local disk; RDS is durable, so the
   snapshot/restore dance in `snapshot.py` can be dropped for the ledger on Postgres.
6. Connections: the DAL opens a connection per request (fine for a local file). For Postgres, add pooling
   (e.g. `psycopg_pool`) so each request leases from a pool instead of a fresh TCP+auth handshake.
7. The app-level exclusive lock (`lifecycle._exclusive`) still works -- it is process-level, not DB-level.

## Scale posture (already adequate, verified)

The pipeline is already reasonable at scale on either substrate: serve predicates and `product(source)` are
indexed, serve lanes paginate, bulk writes are single-transaction, and the `IN (...)` clauses are bounded by
the number of configured SOURCES (a handful), not the product count. No premature chunking/batching was
added -- there is no bottleneck against the real data shape. Revisit only if a genuinely large source list
or a hot unpaginated scan appears.
