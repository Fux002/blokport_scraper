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
    vendor              TEXT NOT NULL DEFAULT '',      -- the company this source belongs to (agnostic)
    origin_default      TEXT NOT NULL DEFAULT '',      -- supplier ISO-2 country
    ports               TEXT,                          -- JSON array of port names / LOCODEs
    mode                TEXT NOT NULL DEFAULT 'review',
    watermarked         INTEGER NOT NULL DEFAULT 0,
    emit_on_review      INTEGER NOT NULL DEFAULT 1,
    default_bundle_size INTEGER NOT NULL DEFAULT 6,
    min_expected_rows   INTEGER NOT NULL DEFAULT 0,
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
    conn.executescript(_SCHEMA)
    return conn


def open_store(path: str | Path | None = None) -> sqlite3.Connection:
    return _connect(Path(path or config_db_path()))


def _row_to_cfg(r: sqlite3.Row) -> SourceConfig:
    return SourceConfig(
        source=r["source"], adapter=r["adapter"], source_code=r["source_code"],
        vendor=r["vendor"], origin_default=r["origin_default"],
        ports_default=json.loads(r["ports"] or "[]"), mode=r["mode"],
        watermarked=bool(r["watermarked"]), emit_on_review=bool(r["emit_on_review"]),
        default_bundle_size=r["default_bundle_size"], min_expected_rows=r["min_expected_rows"],
    )


def _params(cfg: SourceConfig, enabled: bool, schedule: str | None) -> dict:
    return {
        "source": cfg.source, "enabled": 1 if enabled else 0, "schedule": schedule,
        "adapter": cfg.adapter, "source_code": cfg.source_code, "vendor": cfg.vendor,
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
        "origin_default": r["origin_default"], "ports": json.loads(r["ports"] or "[]"),
        "mode": r["mode"], "watermarked": bool(r["watermarked"]),
        "emit_on_review": bool(r["emit_on_review"]),
        "default_bundle_size": r["default_bundle_size"], "min_expected_rows": r["min_expected_rows"],
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


def set_enabled(source: str, enabled: bool, path: str | Path | None = None) -> None:
    with closing(open_store(path)) as conn:
        conn.execute("UPDATE source SET enabled = ?, updated_at = ? WHERE source = ?",
                     (1 if enabled else 0, _now(), source))
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
