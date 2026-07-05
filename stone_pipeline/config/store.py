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
    watermarked         INTEGER NOT NULL DEFAULT 0,
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
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # many readers + one writer, no contention
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
    # durable run records so GET /config/v1/run can return `last` across a config-server restart
    # (the in-memory run dict is lost on restart).
    conn.execute("CREATE TABLE IF NOT EXISTS run_log ("
                 "run_id TEXT PRIMARY KEY, record TEXT NOT NULL, finished_at TEXT)")
    conn.commit()


def open_store(path: str | Path | None = None) -> sqlite3.Connection:
    return _connect(Path(path or config_db_path()))


def _row_to_cfg(r: sqlite3.Row) -> SourceConfig:
    return SourceConfig(
        source=r["source"], adapter=r["adapter"], source_code=r["source_code"],
        vendor=r["vendor"], company_id=r["company_id"], origin_default=r["origin_default"],
        ports_default=json.loads(r["ports"] or "[]"), mode=r["mode"],
        watermarked=bool(r["watermarked"]), emit_on_review=bool(r["emit_on_review"]),
        default_bundle_size=r["default_bundle_size"], min_expected_rows=r["min_expected_rows"],
    )


def _params(cfg: SourceConfig, enabled: bool, schedule: str | None) -> dict:
    return {
        "source": cfg.source, "enabled": 1 if enabled else 0, "schedule": schedule,
        "adapter": cfg.adapter, "source_code": cfg.source_code, "vendor": cfg.vendor,
        "company_id": cfg.company_id,
        "origin_default": cfg.origin_default, "ports": json.dumps(cfg.ports_default or []),
        "mode": cfg.mode, "watermarked": 1 if cfg.watermarked else 0,
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
        "mode": r["mode"], "watermarked": bool(r["watermarked"]),
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
        emit_on_review=bool(data.get("emit_on_review", True)),
        default_bundle_size=int(data.get("default_bundle_size", 6)),
        min_expected_rows=int(data.get("min_expected_rows", 0)),
    )
    upsert_source(cfg, enabled=bool(data.get("enabled", True)),
                  schedule=data.get("schedule"), path=path)


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
                  path: str | Path | None = None) -> None:
    p = _params(cfg, enabled, schedule)
    cols = ", ".join(p)
    placeholders = ", ".join(f":{c}" for c in p)
    updates = ", ".join(f"{c} = :{c}" for c in p if c != "source")
    with closing(open_store(path)) as conn:
        conn.execute(
            f"INSERT INTO source ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(source) DO UPDATE SET {updates}", p)
        conn.commit()


def set_state(source: str, *, lifecycle: str | None = None, enabled: bool | None = None,
              path: str | Path | None = None) -> None:
    """The ONE source-state mutation: set `lifecycle` ('active'|'paused'|'delisted', the label Medusa
    reads) and/or `enabled` (the mechanical run flag) in a single write; either omitted leaves it
    unchanged. The lifecycle verbs go through here."""
    sets: list[str] = []
    params: list = []
    if lifecycle is not None:
        sets.append("lifecycle = ?"); params.append(lifecycle)
    if enabled is not None:
        sets.append("enabled = ?"); params.append(1 if enabled else 0)
    if not sets:
        return
    sets.append("updated_at = ?"); params.append(_now())
    params.append(source)
    with closing(open_store(path)) as conn:
        conn.execute(f"UPDATE source SET {', '.join(sets)} WHERE source = ?", params)
        conn.commit()


def delete_source(source: str, path: str | Path | None = None) -> bool:
    """Remove a scraper from the config store entirely (the ':4200' Remove-permanently button, after its
    ledger products have been purged). Returns True if a row was deleted, False if the source was already
    gone. Config only -- it never touches the ledger; the caller purges the products first."""
    with closing(open_store(path)) as conn:
        n = conn.execute("DELETE FROM source WHERE source = ?", (source,)).rowcount
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


def seed_from_yaml(yaml_path: str | Path | None = None, path: str | Path | None = None) -> int:
    """Seed the store from sources.yaml, INSERT-OR-IGNORE so it never clobbers a row
    the admin already edited. Returns the number of rows inserted."""
    from stone_pipeline.config.sources import load_yaml_sources
    inserted = 0
    with closing(open_store(path)) as conn:
        for cfg in load_yaml_sources(yaml_path).values():
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
