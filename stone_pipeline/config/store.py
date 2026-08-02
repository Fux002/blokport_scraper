"""Scraper config store: the durable control plane for the scrapers.

CONFIG, not state: which scrapers exist, which are enabled, and their per-scraper
agnostic settings (vendor, ports, origin, mode). It lives in its OWN durable SQLite
file, deliberately SEPARATE from the per-env sync ledger (which is regenerated and
would wipe this). The admin UI edits it live; the pipeline reads it. `sources.yaml`
is the committed seed so nothing is lost on day one.

SQLite in WAL mode is the right substrate and cheap: this is read-heavy (the pipeline
reads) with a single writer (the UI), which WAL handles without contention. No
Postgres. The settings are agnostic (names/refs, no Medusa ids), so one config DB
serves both environments. No em dashes (design principle 2).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    source              TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 1,   -- does this scraper run
    schedule            TEXT,                          -- optional: how often / when (free text)
    adapter             TEXT NOT NULL DEFAULT '',
    source_code         TEXT NOT NULL DEFAULT '',
    vendor              TEXT NOT NULL DEFAULT '',      -- the company this source belongs to (agnostic name)
    company_id          TEXT NOT NULL DEFAULT '',      -- Medusa company id for this source (ENV-SPECIFIC; empty = resolve by vendor name)
    origin_default      TEXT NOT NULL DEFAULT '',      -- supplier ISO-2 country
    ports               TEXT,                          -- JSON array of port names / LOCODEs
    mode                TEXT NOT NULL DEFAULT 'review',
    watermarked         INTEGER NOT NULL DEFAULT 0,     -- de-watermark this source's photos (FAL)
    enhance             INTEGER NOT NULL DEFAULT 1,     -- upscale this source's photos (Real-ESRGAN, GPU)
    emit_on_review      INTEGER NOT NULL DEFAULT 1,
    default_bundle_size INTEGER NOT NULL DEFAULT 6,
    min_expected_rows   INTEGER NOT NULL DEFAULT 0,
    last_run_at         TEXT,                          -- when this source was last produced (ISO), for the admin list
    last_run_status     TEXT,                          -- running | succeeded | failed
    last_run_stage      TEXT,                          -- scrape | catalog | inventory | all
    updated_at          TEXT NOT NULL
);
"""


def config_db_path() -> Path:
    """Durable config DB location. BLOKPORT_CONFIG_DB overrides it (tests, ops)."""
    override = os.environ.get("BLOKPORT_CONFIG_DB", "").strip()
    return Path(override) if override else SETTINGS.paths.workspace_root / "config.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    # the SQLite substrate seam (Phase 4 RDS-readiness): a Postgres migration branches HERE, gating the
    # PRAGMAs below. require_sqlite fails loud if a not-yet-wired dialect is configured. See
    # MIGRATION_TO_POSTGRES.md.
    from stone_pipeline.core.dbdialect import require_sqlite
    require_sqlite("config store")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # many readers + one writer, no contention
    conn.execute("PRAGMA busy_timeout=5000")  # ThreadingHTTPServer: two concurrent admin writes wait, not 500
    # apply the DDL once per database, not on every connect (the admin API connects per request)
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for config DBs created before a field existed (the dev
    config.db predates company_id). Cheap PRAGMA check per connect, ALTER only once."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(source)")}
    if "company_id" not in cols:
        conn.execute("ALTER TABLE source ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    for col in ("last_run_at", "last_run_status", "last_run_stage"):   # added for the admin 'last run' label
        if col not in cols:
            conn.execute(f"ALTER TABLE source ADD COLUMN {col} TEXT")
            conn.commit()
    if "lifecycle" not in cols:   # active | paused | delisted -- the label Medusa reads (NULL == active)
        conn.execute("ALTER TABLE source ADD COLUMN lifecycle TEXT")
        conn.commit()
    if "enhance" not in cols:   # per-source GPU upscale switch; DBs created before it default ON
        conn.execute("ALTER TABLE source ADD COLUMN enhance INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    # F8: source_code must be UNIQUE among real codes (the empty default stays non-unique), so two vendors
    # can't collide their SKU namespace -- the app-level read-then-write check races under the threading
    # server. Partial unique index, created idempotently. If a legacy DB already holds a duplicate code the
    # index can't be built: log LOUD and continue rather than brick the control plane on startup (the
    # app-level check still guards new writes; fix the duplicate then restart to activate the constraint).
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_source_code_unique "
                     "ON source(source_code) WHERE source_code != ''")
        conn.commit()
    except sqlite3.IntegrityError:
        log.error("cannot enforce UNIQUE(source_code): the config DB already holds a duplicate non-empty "
                  "source_code; resolve it and restart to activate the constraint")
    # durable run records so GET /config/v1/run can return `last` across a config-server restart
    # (the in-memory run dict is lost on restart).
    conn.execute("CREATE TABLE IF NOT EXISTS run_log ("
                 "run_id TEXT PRIMARY KEY, record TEXT NOT NULL, finished_at TEXT)")
    # retired variation KEYS: the durable exclusion memory (lives in config.db so it is snapshotted +
    # restored, not a CSV under ephemeral /app). The produce reads it so a retired variety is never
    # re-minted or re-matched; un-retire removes the key.
    conn.execute("CREATE TABLE IF NOT EXISTS retired_variation ("
                 "key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    # sources an operator PERMANENTLY removed (Remove-permanently). Durable, so seed_from_yaml does NOT
    # re-add a removed vendor on the next boot (which would silently re-list it). Cleared when the source
    # is re-added via PUT. Without this, reconcile-seeding a partial config.db would resurrect a removal.
    conn.execute("CREATE TABLE IF NOT EXISTS removed_source ("
                 "source TEXT PRIMARY KEY, removed_at TEXT NOT NULL)")
    # -- new-variant review decisions (the durable decision ledger; see config/decisions_store.py) -------
    # One operator decision per uncertain variety spelling, keyed by its NORMALIZED name. `action` unifies
    # what used to be two ephemeral CSVs (variants_to_confirm.csv `confirm` + rejected_varieties.csv):
    #   mint  -> the variety IS real; the next produce mints it
    #   reject-> never propose it again (the learned 'no' memory)
    #   alias -> it is really a spelling of `alias_of` (an existing variety); route the product there
    # In config.db so it is snapshotted + restored (durable on ECS), never a CSV under ephemeral /app.
    # seed_color: an operator-chosen colour to mint a NEW variety with, instead of the generic 'Natural'
    # fallback, when its source supplies no colour (a colourless source would otherwise leave the variety
    # 'Natural' forever, since no scraped product ever carries a colour for the leaf review to surface).
    # seed_type: an operator-assigned stone TYPE to mint a type-less variety with. Type has NO fallback
    # (it drives the Key, so a wrong type is a wrong identity); a variety with no resolvable type is HELD
    # until the operator assigns one here, never guessed.
    # seed_country: the operator-chosen ISO-3166 country of ORIGIN, captured at mint. Origin cannot be
    # inferred from a stone's (marketing) name -- a look-alike is quarried in a new country -- so the true
    # origin is set here at approval and overlaid onto origin_map as a confirmed per-variety rule.
    conn.execute("CREATE TABLE IF NOT EXISTS variety_decision ("
                 "variant_norm TEXT PRIMARY KEY, variant_display TEXT NOT NULL DEFAULT '', "
                 "action TEXT NOT NULL CHECK (action IN ('mint','reject','alias')), "
                 "alias_of TEXT, seed_color TEXT, seed_type TEXT, seed_country TEXT, "
                 "decided_at TEXT NOT NULL)")
    _variety_cols = {r["name"] for r in conn.execute("PRAGMA table_info(variety_decision)")}
    if "seed_color" not in _variety_cols:
        conn.execute("ALTER TABLE variety_decision ADD COLUMN seed_color TEXT")   # DBs created before it
    if "seed_type" not in _variety_cols:
        conn.execute("ALTER TABLE variety_decision ADD COLUMN seed_type TEXT")    # DBs created before it
    if "seed_country" not in _variety_cols:
        conn.execute("ALTER TABLE variety_decision ADD COLUMN seed_country TEXT")  # DBs created before it
    # New colour/finish/type/quality VALUES the operator created in Medusa and pasted the id for, keyed
    # by (kind, normalized value). The next produce adopts the id into the attribute vocab.
    conn.execute("CREATE TABLE IF NOT EXISTS attribute_decision ("
                 "kind TEXT NOT NULL, value_norm TEXT NOT NULL, value_display TEXT NOT NULL DEFAULT '', "
                 "medusa_id TEXT NOT NULL, decided_at TEXT NOT NULL, PRIMARY KEY (kind, value_norm))")
    # The pending review QUEUE: what the last produce surfaced as still-undecided (varieties, attributes,
    # backbone-leaf additions), so the config API can serve it to the admin UI BETWEEN runs without reading
    # the ephemeral review/ dir. Fully rewritten each produce (a decided item just stops reappearing).
    # `payload` is the enriched JSON row; `sources` is a JSON array of the source(s) that proposed it.
    # `kind` is validated in the application (decisions_store.replace_pending), NOT pinned in a DB CHECK,
    # so a new decision kind never needs a schema change.
    conn.execute("CREATE TABLE IF NOT EXISTS review_pending ("
                 "kind TEXT NOT NULL, ref TEXT NOT NULL, "
                 "payload TEXT NOT NULL, sources TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (kind, ref))")
    # migrate DBs created before the queue was generic: drop the legacy CHECK(kind IN ...) (SQLite cannot
    # ALTER a CHECK, so rebuild the table carrying its rows across). No-op once the constraint is gone.
    _pending = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_pending'").fetchone()
    if _pending and "CHECK" in (_pending["sql"] or ""):
        conn.executescript(
            "CREATE TABLE review_pending__new (kind TEXT NOT NULL, ref TEXT NOT NULL, "
            "payload TEXT NOT NULL, sources TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (kind, ref));"
            "INSERT INTO review_pending__new SELECT kind, ref, payload, sources, updated_at FROM review_pending;"
            "DROP TABLE review_pending;"
            "ALTER TABLE review_pending__new RENAME TO review_pending;")
    # Backbone leaf-growth decisions: the operator APPROVES adding a value Medusa already has (quality
    # B/C/D, a colour/finish) onto a variety whose backbone does not allow it yet, disambiguated by
    # stone_type. An approved row becomes a LOAD-TIME OVERLAY on the backbone (loaders.Backbone.
    # apply_leaf_overlay); the committed seed JSON is NEVER mutated, so dropping these rows restores the
    # pristine tree. reject == never propose it again. Durable in config.db, so approvals survive a restart.
    conn.execute("CREATE TABLE IF NOT EXISTS backbone_leaf_decision ("
                 "variety_norm TEXT NOT NULL, stone_type_norm TEXT NOT NULL DEFAULT '', "
                 "attribute TEXT NOT NULL, value_norm TEXT NOT NULL, value_display TEXT NOT NULL DEFAULT '', "
                 "action TEXT NOT NULL CHECK (action IN ('approve','reject')), decided_at TEXT NOT NULL, "
                 "PRIMARY KEY (variety_norm, stone_type_norm, attribute, value_norm))")
    # Operator "Not a duplicate" verdicts: variation Keys the reconcile dedup must NEVER drop, even when a
    # Key shares a (branch,type,name) identity with another. A false-positive dup detection (two genuinely
    # distinct varieties, e.g. multi-type homonyms) is exempted here so a future reconcile/pristine cannot
    # re-tombstone it. Durable in config.db (survives a restart); cleared by a pristine reset like the other
    # curation overlays, so a true cold start is seed-only. Keyed by the variation Key.
    conn.execute("CREATE TABLE IF NOT EXISTS protected_variation ("
                 "key TEXT PRIMARY KEY, decided_at TEXT NOT NULL)")
    # per-source pipeline diagnostics (Phase 1 layer diagnostics): the LATEST run's compact per-stage +
    # health/gate/magnitude/drift summary per source, so GET /config/v1/diagnostics serves the admin UI
    # durably (survives a config-server restart) without scanning the ephemeral outputs/ tree per request.
    # One row per source (latest run wins); the control plane records it after a produce.
    conn.execute("CREATE TABLE IF NOT EXISTS source_diagnostic ("
                 "source TEXT PRIMARY KEY, run_id TEXT NOT NULL, summary TEXT NOT NULL, updated_at TEXT NOT NULL)")
    # rolling per-source run-event history (Phase 2 admission): the last N runs' outcomes, so the admission
    # rule can require N consecutive CLEAN runs before a source is 'consistent enough' to auto-load, and a
    # drift event can auto-demote an auto source. Bounded per source (settings.admission.history_limit).
    conn.execute("CREATE TABLE IF NOT EXISTS source_run_event ("
                 "source TEXT NOT NULL, run_id TEXT NOT NULL, at TEXT NOT NULL, "
                 "health TEXT NOT NULL, worst TEXT NOT NULL, drift TEXT NOT NULL DEFAULT '[]', "
                 "certified INTEGER NOT NULL DEFAULT 0, note TEXT, PRIMARY KEY (source, run_id))")
    conn.commit()


def open_store(path: str | Path | None = None) -> sqlite3.Connection:
    return _connect(Path(path or config_db_path()))


def _row_to_cfg(r: sqlite3.Row) -> SourceConfig:
    return SourceConfig(
        source=r["source"], adapter=r["adapter"], source_code=r["source_code"],
        vendor=r["vendor"], company_id=r["company_id"], origin_default=r["origin_default"],
        ports_default=json.loads(r["ports"] or "[]"), mode=r["mode"],
        watermarked=bool(r["watermarked"]), enhance=bool(r["enhance"]), emit_on_review=bool(r["emit_on_review"]),
        default_bundle_size=r["default_bundle_size"], min_expected_rows=r["min_expected_rows"],
    )


def _params(cfg: SourceConfig, enabled: bool, schedule: str | None) -> dict:
    return {
        "source": cfg.source, "enabled": 1 if enabled else 0, "schedule": schedule,
        "adapter": cfg.adapter, "source_code": cfg.source_code, "vendor": cfg.vendor,
        "company_id": cfg.company_id,
        "origin_default": cfg.origin_default, "ports": json.dumps(cfg.ports_default or []),
        "mode": cfg.mode, "watermarked": 1 if cfg.watermarked else 0, "enhance": 1 if cfg.enhance else 0,
        "emit_on_review": 1 if cfg.emit_on_review else 0,
        "default_bundle_size": cfg.default_bundle_size, "min_expected_rows": cfg.min_expected_rows,
        "updated_at": _now(),
    }


# -- reads (used by the pipeline) ----------------------------------------------

def read_sources(path: str | Path | None = None) -> dict[str, SourceConfig]:
    """Every configured source -> SourceConfig, from the store."""
    with closing(open_store(path)) as conn:
        return {r["source"]: _row_to_cfg(r) for r in conn.execute("SELECT * FROM source")}


def _row_dict(r: sqlite3.Row) -> dict:
    """JSON-friendly row for the admin API (includes enabled + schedule)."""
    return {
        "source": r["source"], "enabled": bool(r["enabled"]), "schedule": r["schedule"],
        "adapter": r["adapter"], "source_code": r["source_code"], "vendor": r["vendor"],
        "company_id": r["company_id"],
        "origin_default": r["origin_default"], "ports": json.loads(r["ports"] or "[]"),
        "mode": r["mode"], "watermarked": bool(r["watermarked"]), "enhance": bool(r["enhance"]),
        "emit_on_review": bool(r["emit_on_review"]),
        "default_bundle_size": r["default_bundle_size"], "min_expected_rows": r["min_expected_rows"],
        # last run (for the admin list's "last run" label); null until this source has ever been produced.
        "last_run_at": r["last_run_at"], "last_run_status": r["last_run_status"],
        "last_run_stage": r["last_run_stage"],
        # lifecycle label Medusa reads: active | paused | delisted. NULL (legacy rows, never paused) == active.
        "lifecycle": r["lifecycle"] or "active",
    }


def list_rows(path: str | Path | None = None) -> list[dict]:
    with closing(open_store(path)) as conn:
        return [_row_dict(r) for r in conn.execute("SELECT * FROM source ORDER BY source")]


def get_row(source: str, path: str | Path | None = None) -> dict | None:
    with closing(open_store(path)) as conn:
        r = conn.execute("SELECT * FROM source WHERE source = ?", (source,)).fetchone()
        return _row_dict(r) if r else None


def upsert_row(data: dict, path: str | Path | None = None) -> None:
    """Upsert from an admin-UI payload dict (the inverse of _row_dict)."""
    cfg = SourceConfig(
        source=data["source"], adapter=data.get("adapter", ""),
        source_code=data.get("source_code", ""), vendor=data.get("vendor", ""),
        company_id=data.get("company_id", ""),
        origin_default=data.get("origin_default", ""), ports_default=data.get("ports") or [],
        mode=data.get("mode", "review"), watermarked=bool(data.get("watermarked", False)),
        enhance=bool(data.get("enhance", True)),
        emit_on_review=bool(data.get("emit_on_review", True)),
        default_bundle_size=int(data.get("default_bundle_size", 6)),
        min_expected_rows=int(data.get("min_expected_rows", 0)),
    )
    # re-adding a source clears any prior removal tombstone (in ONE transaction with the upsert, so a crash
    # between the two can't leave the source added yet still tombstoned).
    upsert_source(cfg, enabled=bool(data.get("enabled", True)),
                  schedule=data.get("schedule"), clear_removed=True, path=path)


def enabled_names(path: str | Path | None = None) -> set[str] | None:
    """The set of enabled source names, or None when there is no config store yet
    (so callers apply no filter and keep the pre-store behaviour)."""
    p = Path(path or config_db_path())
    if not p.exists():
        return None
    with closing(open_store(p)) as conn:
        return {r["source"] for r in conn.execute("SELECT source FROM source WHERE enabled = 1")}


# -- writes (used by the seed + the admin API) ---------------------------------

def upsert_source(cfg: SourceConfig, *, enabled: bool = True, schedule: str | None = None,
                  clear_removed: bool = False, path: str | Path | None = None) -> None:
    p = _params(cfg, enabled, schedule)
    cols = ", ".join(p)
    placeholders = ", ".join(f":{c}" for c in p)
    updates = ", ".join(f"{c} = :{c}" for c in p if c != "source")
    with closing(open_store(path)) as conn:
        conn.execute(
            f"INSERT INTO source ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(source) DO UPDATE SET {updates}", p)
        # F8: clear any prior removal tombstone in the SAME transaction as the source upsert, so a crash
        # can never leave the source inserted but still tombstoned (a re-added vendor stuck as removed).
        if clear_removed:
            conn.execute("DELETE FROM removed_source WHERE source = ?", (cfg.source,))
        conn.commit()


def set_state(source: str, *, lifecycle: str | None = None, enabled: bool | None = None,
              mode: str | None = None, path: str | Path | None = None) -> None:
    """The ONE source-state mutation: set `lifecycle` ('active'|'paused'|'delisted', the label Medusa
    reads), `enabled` (the mechanical run flag), and/or `mode` ('review'|'auto', the trust ladder) in a
    single write; any omitted leaves it unchanged. The lifecycle verbs and the admission auto-demote go
    through here."""
    sets: list[str] = []
    params: list = []
    if lifecycle is not None:
        sets.append("lifecycle = ?"); params.append(lifecycle)
    if enabled is not None:
        sets.append("enabled = ?"); params.append(1 if enabled else 0)
    if mode is not None:
        sets.append("mode = ?"); params.append(mode)
    if not sets:
        return
    sets.append("updated_at = ?"); params.append(_now())
    params.append(source)
    with closing(open_store(path)) as conn:
        conn.execute(f"UPDATE source SET {', '.join(sets)} WHERE source = ?", params)
        conn.commit()


def load_retired(path: str | Path | None = None) -> set[str]:
    """The retired variation KEYS -- the ONE durable exclusion source (in config.db, so snapshotted +
    restored, never a CSV under ephemeral /app). Empty when there is no store yet."""
    p = Path(path or config_db_path())
    if not p.exists():
        return set()
    with closing(open_store(p)) as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM retired_variation")}


def add_retired(key: str, path: str | Path | None = None) -> None:
    with closing(open_store(path)) as conn:
        conn.execute("INSERT OR IGNORE INTO retired_variation (key, created_at) VALUES (?, ?)",
                     (key, _now()))
        conn.commit()


def remove_retired(key: str, path: str | Path | None = None) -> None:
    with closing(open_store(path)) as conn:
        conn.execute("DELETE FROM retired_variation WHERE key = ?", (key,))
        conn.commit()


def clear_retired(path: str | Path | None = None) -> int:
    """Drop EVERY retired variation key (the durable exclusion memory). PRISTINE (factory) reset only: a
    normal reset keeps retirements, but a cold start back to the seed must forget them so a previously
    retired variety is re-minted from the seed like any other. Returns the number of rows dropped."""
    p = Path(path or config_db_path())
    if not p.exists():
        return 0
    with closing(open_store(p)) as conn:
        n = conn.execute("DELETE FROM retired_variation").rowcount
        conn.commit()
    return n


def delete_source(source: str, path: str | Path | None = None) -> bool:
    """Remove a scraper from the config store entirely (the ':4200' Remove-permanently button, after its
    ledger products have been purged). Returns True if a row was deleted, False if the source was already
    gone. Config only -- it never touches the ledger; the caller purges the products first."""
    with closing(open_store(path)) as conn:
        n = conn.execute("DELETE FROM source WHERE source = ?", (source,)).rowcount
        # tombstone the removal so a later reconcile-seed never resurrects this vendor (re-adding it via
        # PUT clears the tombstone -- see upsert_row). Durable in config.db, so it survives a redeploy.
        conn.execute("INSERT OR REPLACE INTO removed_source (source, removed_at) VALUES (?, ?)",
                     (source, _now()))
        conn.commit()
    return n > 0


def record_run_log(record: dict, path: str | Path | None = None) -> None:
    """Persist one run's public record (JSON) so `last` survives a config-server restart. Keyed by
    run_id (upsert), bounded to the most recent 50 by finished_at."""
    with closing(open_store(path)) as conn:
        conn.execute(
            "INSERT INTO run_log (run_id, record, finished_at) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET record = excluded.record, finished_at = excluded.finished_at",
            (record.get("run_id"), json.dumps(record), record.get("finished_at")))
        conn.execute("DELETE FROM run_log WHERE run_id NOT IN "
                     "(SELECT run_id FROM run_log ORDER BY finished_at DESC LIMIT 50)")
        conn.commit()


def last_run_log(path: str | Path | None = None) -> dict | None:
    """The most recent FINISHED run record, or None if none has completed yet."""
    with closing(open_store(path)) as conn:
        r = conn.execute("SELECT record FROM run_log WHERE finished_at IS NOT NULL "
                         "ORDER BY finished_at DESC LIMIT 1").fetchone()
        return json.loads(r["record"]) if r else None


def record_source_diagnostic(source: str, run_id: str, summary: dict,
                             path: str | Path | None = None) -> None:
    """Store one source's latest-run diagnostic summary (compact JSON: per-stage status + health/gate/
    magnitude/drift), upsert by source (latest run wins). Written by the control plane after a produce;
    served by GET /config/v1/diagnostics."""
    with closing(open_store(path)) as conn:
        conn.execute(
            "INSERT INTO source_diagnostic (source, run_id, summary, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET run_id = excluded.run_id, "
            "summary = excluded.summary, updated_at = excluded.updated_at",
            (source, run_id, json.dumps(summary), _now()))
        conn.commit()


def _diag_row(r: sqlite3.Row) -> dict:
    d = json.loads(r["summary"])
    d["updated_at"] = r["updated_at"]
    return d


def get_source_diagnostic(source: str, path: str | Path | None = None) -> dict | None:
    """One source's latest persisted diagnostic, or None. Guarded so a read never CREATES the config DB
    (the test-isolation contract keeps an absent DB absent)."""
    p = Path(path or config_db_path())
    if not p.exists():
        return None
    with closing(open_store(p)) as conn:
        r = conn.execute("SELECT summary, updated_at FROM source_diagnostic WHERE source = ?",
                         (source,)).fetchone()
        return _diag_row(r) if r else None


def read_source_diagnostics(path: str | Path | None = None) -> list[dict]:
    """Every source's latest persisted diagnostic (empty when there is no store yet)."""
    p = Path(path or config_db_path())
    if not p.exists():
        return []
    with closing(open_store(p)) as conn:
        return [_diag_row(r) for r in
                conn.execute("SELECT summary, updated_at FROM source_diagnostic ORDER BY source")]


def record_run_event(source: str, run_id: str, health: str, worst: str, drift: list,
                     certified: bool, note: str | None = None, limit: int = 20,
                     path: str | Path | None = None) -> None:
    """Append one run's outcome to a source's rolling history (upsert by run_id), then prune to the newest
    `limit`. Feeds the admission rule (N consecutive clean runs) and the auto-demote-on-drift reaction."""
    with closing(open_store(path)) as conn:
        conn.execute(
            "INSERT INTO source_run_event (source, run_id, at, health, worst, drift, certified, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, run_id) DO UPDATE SET at = excluded.at, health = excluded.health, "
            "worst = excluded.worst, drift = excluded.drift, certified = excluded.certified, note = excluded.note",
            (source, run_id, _now(), health, worst, json.dumps(drift), 1 if certified else 0, note))
        conn.execute(
            "DELETE FROM source_run_event WHERE source = ? AND run_id NOT IN "
            "(SELECT run_id FROM source_run_event WHERE source = ? ORDER BY at DESC, run_id DESC LIMIT ?)",
            (source, source, limit))
        conn.commit()


def read_run_events(source: str, limit: int | None = None,
                    path: str | Path | None = None) -> list[dict]:
    """A source's run history, newest first (empty when there is no store yet)."""
    p = Path(path or config_db_path())
    if not p.exists():
        return []
    query = "SELECT * FROM source_run_event WHERE source = ? ORDER BY at DESC, run_id DESC"
    params: tuple = (source,)
    if limit:
        query += " LIMIT ?"
        params = (source, limit)
    with closing(open_store(p)) as conn:
        return [{"source": r["source"], "run_id": r["run_id"], "at": r["at"], "health": r["health"],
                 "worst": r["worst"], "drift": json.loads(r["drift"] or "[]"),
                 "certified": bool(r["certified"]), "note": r["note"]}
                for r in conn.execute(query, params)]


def record_run(sources, status: str, stage: str, at: str | None = None,
               path: str | Path | None = None) -> None:
    """Stamp each named source with its most recent run (time, status, stage) for the admin list's
    'last run' label. Durable in config.db, so it survives a config-server restart (unlike the
    in-memory run record). An unknown source name simply matches no row (no-op)."""
    at = at or _now()
    with closing(open_store(path)) as conn:
        conn.executemany(
            "UPDATE source SET last_run_at = ?, last_run_status = ?, last_run_stage = ?, "
            "updated_at = ? WHERE source = ?",
            [(at, status, stage, at, s) for s in sources])
        conn.commit()


def reconcile_interrupted_runs(path: str | Path | None = None) -> list[str]:
    """A source stamped `running` at server boot has no owning process: the in-memory run record and its
    produce subprocess died with the PRIOR config server (a redeploy, crash, or spot reclaim killed the run
    mid-produce). The durable stamp is therefore stale -- a fresh boot never has an in-flight run. Mark it
    `interrupted` so it reads terminal and the source is runnable again, instead of being pinned `running`
    forever. Called once at startup after the snapshot restore. Returns the reconciled source names."""
    with closing(open_store(path)) as conn:
        names = [r["source"] for r in
                 conn.execute("SELECT source FROM source WHERE last_run_status = 'running'")]
        if names:
            conn.execute(
                "UPDATE source SET last_run_status = 'interrupted', updated_at = ? "
                "WHERE last_run_status = 'running'", (_now(),))
            conn.commit()
        return names


def seed_from_yaml(yaml_path: str | Path | None = None, path: str | Path | None = None) -> int:
    """RECONCILE the store's source LIST with sources.yaml, INSERT-OR-IGNORE so it never clobbers a row
    the admin already edited (a pause/delist/last-run stays put). Safe to run on EVERY boot: it re-adds
    any yaml source missing from the store -- e.g. after restoring a partial/stale config.db snapshot, so
    the source list can never silently shrink. Sources an operator PERMANENTLY removed are skipped (their
    removed_source tombstone), so a removal is never resurrected. Returns the number of rows inserted."""
    from stone_pipeline.config.sources import load_yaml_sources
    inserted = 0
    with closing(open_store(path)) as conn:
        removed = {r["source"] for r in conn.execute("SELECT source FROM removed_source")}
        for cfg in load_yaml_sources(yaml_path).values():
            if cfg.source in removed:
                continue                              # explicitly removed -> do NOT re-seed it
            p = _params(cfg, enabled=True, schedule=None)
            cols = ", ".join(p)
            placeholders = ", ".join(f":{c}" for c in p)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO source ({cols}) VALUES ({placeholders})", p)
            inserted += cur.rowcount
        conn.commit()
    return inserted


def main(argv: list[str] | None = None) -> int:
    import sys

    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "list"
    if cmd == "seed":
        n = seed_from_yaml()
        print(f"seeded {n} sources into {config_db_path()}")
        return 0
    if cmd == "list":
        if not config_db_path().exists():
            print(f"no config store at {config_db_path()} (run: python -m stone_pipeline.config.store seed)")
            return 1
        for name, cfg in sorted(read_sources().items()):
            en = enabled_names() or set()
            flag = "on " if name in en else "off"
            print(f"  [{flag}] {name:<14} vendor={cfg.vendor!r} code={cfg.source_code} mode={cfg.mode}")
        return 0
    print(f"unknown command {cmd!r}; expected seed or list")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
