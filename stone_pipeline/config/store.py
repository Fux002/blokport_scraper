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
from stone_pipeline.core import env
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.store")

# The retired enhancer's per-source settings (owner 2026-11-02): dropped from config.db, ignored when an admin sends them.
_RETIRED_FIELDS = frozenset({"enhance", "fal_prompt", "watermarked"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    source              TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 1,   -- does this scraper run
    source_code         TEXT NOT NULL DEFAULT '',
    vendor              TEXT NOT NULL DEFAULT '',      -- the company this source belongs to (agnostic name)
    company_id          TEXT NOT NULL DEFAULT '',      -- Medusa company id for this source (ENV-SPECIFIC; empty = resolve by vendor name)
    origin_default      TEXT NOT NULL DEFAULT '',      -- supplier ISO-2 country (provenance fallback)
    primary_origin      TEXT NOT NULL DEFAULT '',      -- vendor's PRIMARY quarry ISO-2 (opt-in origin-confirmation gate)
    collection_country  TEXT NOT NULL DEFAULT '',      -- supplier collection ISO-2 country (independent of origin)
    collection_city     TEXT NOT NULL DEFAULT '',      -- supplier collection city
    ports               TEXT,                          -- JSON array of port names / LOCODEs
    mode                TEXT NOT NULL DEFAULT 'review',
    image_pipeline      INTEGER NOT NULL DEFAULT 1,     -- the photo process + no_photo rule (off: scraped photos)
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
    override = env.getenv("BLOKPORT_CONFIG_DB", "").strip()
    return Path(override) if override else SETTINGS.paths.workspace_root / "config.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfigStoreError(RuntimeError):
    """A config.db this code will not open (one from before phase 1): nothing is read or written."""


# Bump when _migrate gains a step: a database stamped at this version skips the schema work entirely, so a
# new step never reaches an already-stamped database unless the version moves (same pattern as ledger/db.py).
SCHEMA_VERSION = 13    # 4: the decision `statement` table + its one-time migration; 5: the legacy tables dropped;
#                        6: source.lots (a per-vendor lot switch); 7: lot_sold_answer; 8: source.lots dropped;
#                        9: source.image_pipeline (on for every source); the retired enhancer's columns dropped;
#                        10: every source with enabled 0 is labelled (lifecycle 'paused' unless paused/delisted);
#                        11: backbone_leaf_decision.decided_by (operator | produce);
#                        12: phase 1 (PHASE-1-SPEC rev 8): retired and protected Keys are new variety Keys. The
#                            tables are unchanged, so the version alone tells a migrated store from an old one:
#                            ONLY the phase 1 store migration (phase1.migrate_stores) stamps 12, and a store at
#                            1 to 11 is refused here (report 18 N3).
#                        13: P2-10 C2: the learned_alias table (additive, so a phase 1 store opens in place).
# The version the phase 1 store migration writes (phase1/store_writer.py). It stays 12 whatever SCHEMA_VERSION
# becomes: that migration copies an old store without running _migrate, so stamping the current version would mark
# a store that lacks every later table as complete. The first open then upgrades it in place.
PHASE1_SCHEMA_VERSION = 12


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Per-connection setup: row access + pragmas every time, the schema work ONCE per database. A brand-new database
    (version 0) gets the base DDL and every step, then the stamp; a stamped one runs nothing but the pragmas (the admin
    API connects per request, decisions_store per call). A database from before phase 1 (1 to 11) is REFUSED: its Keys
    are old Keys, and only the phase 1 store migration may turn them into new ones. A phase 1 database (12 up to the
    current version) is upgraded in place: every later step is additive."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # many readers + one writer, no contention
    conn.execute("PRAGMA busy_timeout=5000")  # ThreadingHTTPServer: two concurrent admin writes wait, not 500
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if 0 < version < PHASE1_SCHEMA_VERSION:
        raise ConfigStoreError(f"config.db schema {version} predates phase 1 ({PHASE1_SCHEMA_VERSION}): run the "
                               "store migration (RUN_MODE=phase1-migrate), never open it in place")
    if version < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


def _connect(path: Path) -> sqlite3.Connection:
    # the SQLite substrate seam (Phase 4 RDS-readiness): a Postgres migration branches HERE, gating the
    # PRAGMAs in _prepare. require_sqlite fails loud if a not-yet-wired dialect is configured. See
    # MIGRATION_TO_POSTGRES.md.
    from stone_pipeline.core.dbdialect import require_sqlite
    require_sqlite("config store")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        return _prepare(conn)
    except Exception:
        # a refused migration leaves NOTHING behind: its partial writes roll back and the lock is released,
        # so the next open (after the operator's fix) starts from the untouched legacy rows
        conn.rollback()
        conn.close()
        raise


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
    # the label is exactly active | paused | delisted: a legacy row with the run flag off and no label (or 'active',
    # from a settings save that flipped the flag alone) is a paused source. Idempotent.
    conn.execute("UPDATE source SET lifecycle = 'paused' "
                 "WHERE enabled = 0 AND (lifecycle IS NULL OR lifecycle = 'active')")
    conn.commit()
    if "image_pipeline" not in cols:   # per-source switch of the photo process: ON for every existing source (owner
        # 2026-11-02; the retired enhance is NOT carried over: its zeros only stopped the old enhancer's spend)
        conn.execute("ALTER TABLE source ADD COLUMN image_pipeline INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    for retired in sorted(_RETIRED_FIELDS & cols):   # the retired enhancer's settings (owner 2026-11-02)
        conn.execute(f"ALTER TABLE source DROP COLUMN {retired}")
        conn.commit()
    if "primary_origin" not in cols:   # vendor primary quarry country (opt-in origin gate); older rows blank
        conn.execute("ALTER TABLE source ADD COLUMN primary_origin TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "collection_country" not in cols:   # supplier collection location (independent of origin); older rows blank
        conn.execute("ALTER TABLE source ADD COLUMN collection_country TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "collection_city" not in cols:
        conn.execute("ALTER TABLE source ADD COLUMN collection_city TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "lots" in cols:   # the retired per-vendor lot switch: the loader decides lots from the data alone
        conn.execute("ALTER TABLE source DROP COLUMN lots")
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
    # One-off data migrations record themselves here so they run exactly once (see _migrate_statements).
    conn.execute("CREATE TABLE IF NOT EXISTS migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    # THE decision table (DECISION_MODEL_DESIGN.md section 2.1): one STATEMENT per (vendor, spelling) -- what the
    # listing IS (name, type, colour, origin, widen) or 'reject'. Vendor '' is the spelling's global meaning.
    # Whether a statement mints or binds is derived at read time (config.decisions_model), never stored.
    # KEY CONTRACT: `spelling_norm` is ALWAYS the normalized SCRAPED spelling of a listing (what the vendor
    # wrote), never a variety name; `name` is the variety it means. The one statement whose spelling equals
    # a variety name is "vendor's X is X" (an origin confirmation on a listing already spelled X).
    conn.execute("CREATE TABLE IF NOT EXISTS statement ("
                 "source TEXT NOT NULL, spelling_norm TEXT NOT NULL, spelling TEXT NOT NULL, "
                 "verdict TEXT NOT NULL CHECK (verdict IN ('is', 'reject')), "
                 "name TEXT, stone_type TEXT, color TEXT, origin_iso TEXT, "
                 "widen INTEGER NOT NULL DEFAULT 0, asked_by TEXT NOT NULL DEFAULT '', "
                 "decided_at TEXT NOT NULL, PRIMARY KEY (source, spelling_norm))")
    _migrate_statements(conn)
    _drop_legacy_decision_tables(conn)
    # Operator-edited PER-VARIETY origins (the "edit origins" admin action, same channel as a mint's
    # seed_country but for any variety and holding a LIST of countries). Keyed by (normalized variety,
    # normalized type). country_iso is a comma-list ("IN,IR"). Overlaid onto the origin MAP at load, so
    # derive resolves the variety against the edited countries and the per-vendor gate picks from the list.
    conn.execute("CREATE TABLE IF NOT EXISTS variety_origin ("
                 "variant_norm TEXT NOT NULL, stone_type_norm TEXT NOT NULL, "
                 "variant_display TEXT NOT NULL DEFAULT '', country_iso TEXT NOT NULL, "
                 "city TEXT NOT NULL DEFAULT '', county TEXT NOT NULL DEFAULT '', "
                 "decided_at TEXT NOT NULL, PRIMARY KEY (variant_norm, stone_type_norm))")
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
    # Lot sold answers: which scanned slabs of a lot are sold, stated by the operator on the "which slabs are
    # sold?" card for the stock figure it was asked at (stages/lots.sold_positions uses it only while that
    # figure still holds). A fact about the vendor's stock, not catalog identity, so no reset clears it.
    conn.execute("CREATE TABLE IF NOT EXISTS lot_sold_answer ("
                 "source TEXT NOT NULL, lot_ref TEXT NOT NULL, available INTEGER NOT NULL, "
                 "sold TEXT NOT NULL, decided_at TEXT NOT NULL, PRIMARY KEY (source, lot_ref))")
    # Backbone leaf-growth decisions: the operator APPROVES adding a value Medusa already has (quality
    # B/C/D, a colour/finish) onto a variety whose backbone does not allow it yet, disambiguated by
    # stone_type. An approved row becomes a LOAD-TIME OVERLAY on the backbone (loaders.Backbone.
    # apply_leaf_overlay); the committed seed JSON is NEVER mutated, so dropping these rows restores the
    # pristine tree. reject == never propose it again. Durable in config.db, so approvals survive a restart.
    # `decided_by`: 'operator' (a verdict from the review API) or 'produce' (a minted variety's own membership, which
    # the produce records while it runs, leaves.record_minted_membership); only the operator's are decisions a produce
    # has yet to apply (decisions_pending_apply). For the rows from before the column, the migration stamps those
    # written inside a produce run as the produce's; everything else stays the operator's (_stamp_produce_membership).
    conn.execute("CREATE TABLE IF NOT EXISTS backbone_leaf_decision ("
                 "variety_norm TEXT NOT NULL, stone_type_norm TEXT NOT NULL DEFAULT '', "
                 "attribute TEXT NOT NULL, value_norm TEXT NOT NULL, value_display TEXT NOT NULL DEFAULT '', "
                 "action TEXT NOT NULL CHECK (action IN ('approve','reject')), decided_at TEXT NOT NULL, "
                 "decided_by TEXT NOT NULL DEFAULT 'operator' CHECK (decided_by IN ('operator','produce')), "
                 "PRIMARY KEY (variety_norm, stone_type_norm, attribute, value_norm))")
    if "decided_by" not in {r["name"] for r in conn.execute("PRAGMA table_info(backbone_leaf_decision)")}:
        conn.execute("ALTER TABLE backbone_leaf_decision ADD COLUMN decided_by TEXT NOT NULL DEFAULT 'operator' "
                     "CHECK (decided_by IN ('operator','produce'))")
    # the stamp is its own recorded step (not keyed on the column existing): a stamp that failed after the column was
    # added runs again on the next open instead of being skipped
    if not conn.execute("SELECT 1 FROM migration WHERE name = ?", (_LEAF_DECIDED_BY_MIGRATION,)).fetchone():
        _stamp_produce_membership(conn)
        conn.execute("INSERT INTO migration (name, applied_at) VALUES (?, ?)", (_LEAF_DECIDED_BY_MIGRATION, _now()))
        conn.commit()
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
    # Learned aliases (P2-10 C2; shape frozen by the scraper manager 11-12): a spelling the pipeline GUESSED for a
    # variety, keyed by its identity Key and norm = core.text.match_key(value). The matcher and curate write
    # 'proposed'; only an 'active' row is a matcher input; a 'rejected' norm is never proposed again for that Key
    # (the primary key holds it). `method` is how it was learned, `source` the vendor whose listing proposed it,
    # `score` the proposing arm's own score on 0..100 for the review card (PHASE-2-SPEC:176), NULL where the arm has
    # none and for a migrated row. No reset clears it: only a review decision changes a row.
    conn.execute("CREATE TABLE IF NOT EXISTS learned_alias ("
                 "identity_key TEXT NOT NULL, value TEXT NOT NULL, norm TEXT NOT NULL, method TEXT NOT NULL, "
                 "source TEXT NOT NULL, "
                 "status TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'rejected')), "
                 "first_seen TEXT NOT NULL, decided_by TEXT, decided_at TEXT, score REAL, "
                 "PRIMARY KEY (identity_key, norm))")
    conn.commit()


_LEAF_DECIDED_BY_MIGRATION = "leaf_decided_by_v1"


def _stamp_produce_membership(conn: sqlite3.Connection) -> None:
    """The decided_by migration: an 'approve' row written inside a FINISHED catalog-applying run's [started_at,
    finished_at] window, whatever the run's outcome (the produce records membership as it curates, before it can
    fail, stop or be interrupted), is a minted variety's membership the produce recorded, so it is stamped 'produce';
    every other row keeps 'operator'. Without this, every membership row of the last produce would count as a
    pending operator decision right after the deploy. Reads the bounded run_log (<=50 runs)."""
    windows = [(rec["started_at"], rec["finished_at"]) for rec in map(json.loads, (
        r["record"] for r in conn.execute("SELECT record FROM run_log WHERE finished_at IS NOT NULL")))
        if rec.get("stage") in _APPLIED_STAGES and rec.get("started_at")]
    for started_at, finished_at in windows:
        conn.execute("UPDATE backbone_leaf_decision SET decided_by = 'produce' "
                     "WHERE action = 'approve' AND decided_at BETWEEN ? AND ?", (started_at, finished_at))


STATEMENTS_MIGRATION = "statements_v1"


def _migrate_statements(conn: sqlite3.Connection) -> None:
    """The legacy decision tables -> `statement`, ONCE (recorded in `migration`; DECISION_MODEL_DESIGN.md
    section 4). Structural, no variety lookups, so it runs here on the first open. A mint row is an 'is'
    statement (its corrected name, type, colour, country); a reject row a 'reject' statement; a vendor binding
    with no statement yet is an 'is' statement naming its target; a vendor origin lands on the statement whose
    target it names, else becomes the statement "this vendor's <name> is <name>, from <origin>" (the origin-queue
    confirmation it was). Row-for-row: every legacy row is accounted for or the migration refuses.
    The legacy tables are dropped right after (_drop_legacy_decision_tables); nothing reads them."""
    if conn.execute("SELECT 1 FROM migration WHERE name = ?", (STATEMENTS_MIGRATION,)).fetchone():
        return
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"variety_decision", "scoped_alias", "origin_decision"} <= tables:
        conn.execute("INSERT OR IGNORE INTO migration (name, applied_at) VALUES (?, ?)",
                     (STATEMENTS_MIGRATION, _now()))
        return                                                   # a brand-new store: nothing to migrate
    # the legacy tables in their LAST shape only (two levels: source + asked_by); an older config.db must be
    # opened by a release that still carried those column migrations (any before the statement table)
    if "asked_by" not in {r["name"] for r in conn.execute("PRAGMA table_info(variety_decision)")}:
        raise RuntimeError("config.db holds a pre-two-levels variety_decision table this release cannot migrate; "
                           "open it once with a release before 48672a6, then upgrade")
    now = _now()
    moved = {"mint": 0, "reject": 0, "binding": 0, "implied": 0, "origin": 0}
    for r in conn.execute("SELECT source, variant_norm, variant_display, action, seed_color, seed_type, "
                          "seed_country, seed_name, asked_by FROM variety_decision").fetchall():
        if r["action"] == "reject":
            conn.execute("INSERT OR REPLACE INTO statement (source, spelling_norm, spelling, verdict, asked_by, "
                         "decided_at) VALUES (?, ?, ?, 'reject', ?, ?)",
                         (r["source"] or "", r["variant_norm"], r["variant_display"] or r["variant_norm"],
                          r["asked_by"] or "", now))
            moved["reject"] += 1
        elif r["action"] == "mint":
            conn.execute("INSERT OR REPLACE INTO statement (source, spelling_norm, spelling, verdict, name, "
                         "stone_type, color, origin_iso, asked_by, decided_at) VALUES (?, ?, ?, 'is', ?, ?, ?, ?, ?, ?)",
                         (r["source"] or "", r["variant_norm"], r["variant_display"] or r["variant_norm"],
                          r["seed_name"], r["seed_type"], r["seed_color"], r["seed_country"],
                          r["asked_by"] or r["source"] or "", now))
            moved["mint"] += 1
    for r in conn.execute("SELECT source, variant_norm, variant_display, alias_of, seed_type FROM scoped_alias").fetchall():
        if conn.execute("SELECT 1 FROM statement WHERE source = ? AND spelling_norm = ?",
                        (r["source"], r["variant_norm"])).fetchone():
            moved["implied"] += 1                                # the vendor mint already implies the binding
            continue
        conn.execute("INSERT INTO statement (source, spelling_norm, spelling, verdict, name, stone_type, asked_by, "
                     "decided_at) VALUES (?, ?, ?, 'is', ?, ?, ?, ?)",
                     (r["source"], r["variant_norm"], r["variant_display"] or r["variant_norm"],
                      r["alias_of"], r["seed_type"], r["source"], now))
        moved["binding"] += 1
    from stone_pipeline.matching import projections as proj     # the SAME normalization the legacy keys used
    for r in conn.execute("SELECT source, variant_norm, stone_type_norm, variant_display, country_iso, widen "
                          "FROM origin_decision").fetchall():
        # the vendor's own statement, or the global one its card produced (decide() wrote the origin row
        # next to a global mint keyed by the asking vendor): whichever names this (name, type) as its target
        hit = next((s for s in conn.execute(
            "SELECT source, spelling_norm, spelling, name, stone_type FROM statement "
            "WHERE verdict = 'is' AND (source = ? OR (source = '' AND asked_by = ?))",
            (r["source"], r["source"])).fetchall()
            if proj.norm(s["name"] or s["spelling"]) == r["variant_norm"]
            and proj.norm(s["stone_type"] or "") == r["stone_type_norm"]), None)
        if hit:
            conn.execute("UPDATE statement SET origin_iso = ?, widen = ? WHERE source = ? AND spelling_norm = ?",
                         (r["country_iso"], r["widen"], hit["source"], hit["spelling_norm"]))
        else:
            # the statement "this vendor's <name> is <name>": its spelling IS the variety name. That slot must
            # be free: a vendor statement already there means the same spelling was decided as ANOTHER stone,
            # and replacing it would silently drop that decision. Refuse and name the rows.
            taken = conn.execute("SELECT name, stone_type FROM statement WHERE source = ? AND spelling_norm = ?",
                                 (r["source"], r["variant_norm"])).fetchone()
            if taken:
                raise RuntimeError(
                    f"statement migration: {r['source']}'s origin decision on {r['variant_display'] or r['variant_norm']} "
                    f"({r['stone_type_norm']}) collides with that vendor's statement on the same spelling "
                    f"({taken['name']}, {taken['stone_type']}); resolve it in config.db, then restart")
            conn.execute("INSERT INTO statement (source, spelling_norm, spelling, verdict, name, stone_type, "
                         "origin_iso, widen, asked_by, decided_at) VALUES (?, ?, ?, 'is', ?, ?, ?, ?, ?, ?)",
                         (r["source"], r["variant_norm"], r["variant_display"] or r["variant_norm"],
                          r["variant_display"] or r["variant_norm"], r["stone_type_norm"], r["country_iso"],
                          r["widen"], r["source"], now))
        moved["origin"] += 1
    # row for row: every legacy row is a statement, an update on one, or a binding a vendor mint implies
    legacy_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("variety_decision", "scoped_alias", "origin_decision")}
    accounted = {"variety_decision": moved["mint"] + moved["reject"],
                 "scoped_alias": moved["binding"] + moved["implied"], "origin_decision": moved["origin"]}
    if accounted != legacy_counts:
        raise RuntimeError(f"statement migration did not account for every legacy row: moved {accounted}, "
                           f"legacy {legacy_counts}; refusing to record it")
    conn.execute("INSERT OR IGNORE INTO migration (name, applied_at) VALUES (?, ?)", (STATEMENTS_MIGRATION, now))
    conn.commit()
    log.warning("decision statements migrated from the legacy tables", extra={"extra_fields": moved})


_LEGACY_DECISION_TABLES = ("variety_decision", "scoped_alias", "origin_decision", "variety_decision__pre_two_levels")


def _drop_legacy_decision_tables(conn: sqlite3.Connection) -> None:
    """Remove the tables the statement migration replaced, once it is recorded (schema version 5). A store
    migrated by the previous release still carries them; a fresh store never had them."""
    if not conn.execute("SELECT 1 FROM migration WHERE name = ?", (STATEMENTS_MIGRATION,)).fetchone():
        raise RuntimeError("the legacy decision tables outlive an unrecorded statement migration; refusing")
    for table in _LEGACY_DECISION_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def migration_applied(name: str, path: str | Path | None = None) -> bool:
    with closing(open_store(path)) as conn:
        return conn.execute("SELECT 1 FROM migration WHERE name = ?", (name,)).fetchone() is not None


def open_store(path: str | Path | None = None) -> sqlite3.Connection:
    """A connection for WRITING (creates the store when absent): the config server's boot (restore, then seed) and
    its admin writes. A produce-path writer uses open_existing_store instead."""
    return _connect(Path(path or config_db_path()))


def open_existing_store(path: str | Path | None = None) -> sqlite3.Connection:
    """THE produce-side opener: a connection for WRITING to a config.db that must already exist. The store is created
    only by the config server's boot (restore from S3, then seed from sources.yaml), never by a produce write: a store
    a write created would be EMPTY of sources, hiding sources.yaml from load_sources, and on the service a present
    local file makes the boot skip its restore (a stale store). Absent raises ConfigStoreError and creates nothing."""
    p = Path(path or config_db_path())
    if not p.exists():
        raise ConfigStoreError(f"config.db absent at {p}: a produce writes only into an existing store (the config "
                               "server creates it: restore, then seed)")
    return _connect(p)


def read_store(path: str | Path | None = None) -> sqlite3.Connection:
    """A connection for READING: the store when it exists, else an EMPTY in-memory schema. A read on a host
    without config.db (a produce on a fresh task, load_all on a laptop) must never materialise one: an empty
    file would shadow the sources.yaml seed for load_source. Every read-only accessor goes through here, so
    no reader needs its own exists() guard."""
    p = Path(path or config_db_path())
    if p.exists():
        return _connect(p)
    from stone_pipeline.core.dbdialect import require_sqlite
    require_sqlite("config store")
    return _prepare(sqlite3.connect(":memory:"))


def _row_to_cfg(r: sqlite3.Row) -> SourceConfig:
    return SourceConfig(
        source=r["source"], source_code=r["source_code"],
        vendor=r["vendor"], company_id=r["company_id"], origin_default=r["origin_default"],
        primary_origin=r["primary_origin"],
        collection_country_default=r["collection_country"], collection_city_default=r["collection_city"],
        ports_default=json.loads(r["ports"] or "[]"), mode=r["mode"],
        image_pipeline=bool(r["image_pipeline"]), emit_on_review=bool(r["emit_on_review"]),
        default_bundle_size=r["default_bundle_size"], min_expected_rows=r["min_expected_rows"],
    )


def _params(cfg: SourceConfig, enabled: bool) -> dict:
    return {
        "source": cfg.source, "enabled": 1 if enabled else 0,
        "source_code": cfg.source_code, "vendor": cfg.vendor,
        "company_id": cfg.company_id,
        "origin_default": cfg.origin_default, "primary_origin": cfg.primary_origin,
        "ports": json.dumps(cfg.ports_default or []),
        "collection_country": cfg.collection_country_default, "collection_city": cfg.collection_city_default,
        "mode": cfg.mode, "image_pipeline": 1 if cfg.image_pipeline else 0,
        "emit_on_review": 1 if cfg.emit_on_review else 0,
        "default_bundle_size": cfg.default_bundle_size, "min_expected_rows": cfg.min_expected_rows,
        "updated_at": _now(),
    }


# -- reads (used by the pipeline) ----------------------------------------------

def read_sources(path: str | Path | None = None) -> dict[str, SourceConfig]:
    """Every configured source -> SourceConfig, from the store."""
    with closing(open_store(path)) as conn:
        return {r["source"]: _row_to_cfg(r) for r in conn.execute("SELECT * FROM source")}


_LIFECYCLE_OFF = ("paused", "delisted")


def effective_lifecycle(lifecycle: str | None, enabled) -> str:
    """THE derivation of a source's lifecycle, exactly one of active | paused | delisted, read by the admin listing
    (_row_dict) and the run guard (inactive_sources) alike, so neither can call a source active while the other
    refuses it: the stored lifecycle when it is paused or delisted; else 'active' when the run flag is on, 'paused'
    when it is off (schema 10 labels such rows and set_state never writes one, so this is the reading's own rule)."""
    if lifecycle in _LIFECYCLE_OFF:
        return lifecycle
    return "active" if enabled else "paused"


def _row_dict(r: sqlite3.Row) -> dict:
    """JSON-friendly row for the admin API (includes enabled)."""
    return {
        "source": r["source"], "enabled": bool(r["enabled"]),
        "source_code": r["source_code"], "vendor": r["vendor"],
        "company_id": r["company_id"],
        "origin_default": r["origin_default"], "primary_origin": r["primary_origin"],
        "ports": json.loads(r["ports"] or "[]"),
        "collection_country": r["collection_country"], "collection_city": r["collection_city"],
        "mode": r["mode"], "image_pipeline": bool(r["image_pipeline"]),
        "emit_on_review": bool(r["emit_on_review"]),
        "default_bundle_size": r["default_bundle_size"], "min_expected_rows": r["min_expected_rows"],
        # last run (for the admin list's "last run" label); null until this source has ever been produced.
        "last_run_at": r["last_run_at"], "last_run_status": r["last_run_status"],
        "last_run_stage": r["last_run_stage"],
        # lifecycle label Medusa reads: exactly active | paused | delisted (see effective_lifecycle)
        "lifecycle": effective_lifecycle(r["lifecycle"], r["enabled"]),
    }


def list_rows(path: str | Path | None = None) -> list[dict]:
    with closing(open_store(path)) as conn:
        return [_row_dict(r) for r in conn.execute("SELECT * FROM source ORDER BY source")]


def get_row(source: str, path: str | Path | None = None) -> dict | None:
    with closing(open_store(path)) as conn:
        r = conn.execute("SELECT * FROM source WHERE source = ?", (source,)).fetchone()
        return _row_dict(r) if r else None


def upsert_row(data: dict, path: str | Path | None = None) -> None:
    """Upsert from an admin-UI payload dict (the inverse of _row_dict). The retired enhancer's fields an admin built before
    their removal still sends are ignored, as every unknown field is, and named in a warning so the leftover is seen.
    `enabled` and `lifecycle` are not settings: set_state owns them (the lifecycle verbs), so they are not read here."""
    if retired := sorted(_RETIRED_FIELDS & set(data)):
        log.warning(f"source {data.get('source')!r}: ignored the retired field(s) {', '.join(retired)}")
    cfg = SourceConfig(
        source=data["source"],
        source_code=data.get("source_code", ""), vendor=data.get("vendor", ""),
        company_id=data.get("company_id", ""),
        origin_default=data.get("origin_default", ""), primary_origin=data.get("primary_origin", ""),
        ports_default=data.get("ports") or [],
        collection_country_default=data.get("collection_country", ""),
        collection_city_default=data.get("collection_city", ""),
        mode=data.get("mode", "review"), image_pipeline=bool(data.get("image_pipeline", True)),
        emit_on_review=bool(data.get("emit_on_review", True)),
        default_bundle_size=int(data.get("default_bundle_size", 6)),
        min_expected_rows=int(data.get("min_expected_rows", 0)),
    )
    # re-adding a source clears any prior removal tombstone (in ONE transaction with the upsert, so a crash
    # between the two can't leave the source added yet still tombstoned).
    upsert_source(cfg, clear_removed=True, path=path)


def inactive_sources(names, path: str | Path | None = None) -> dict[str, str]:
    """{name: state} for each of `names` whose effective_lifecycle is not 'active' (paused or delisted).
    Names the store does not hold, or an absent store, are not inactive (a pure read: it never creates config.db)."""
    names = list(names)
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    with closing(read_store(path)) as conn:
        rows = conn.execute(f"SELECT source, enabled, lifecycle FROM source WHERE source IN ({marks})", names)
        states = {r["source"]: effective_lifecycle(r["lifecycle"], r["enabled"]) for r in rows}
    return {name: state for name, state in states.items() if state != "active"}


def enabled_names(path: str | Path | None = None) -> set[str] | None:
    """The set of enabled source names, or None when there is no config store yet
    (so callers apply no filter and keep the pre-store behaviour)."""
    p = Path(path or config_db_path())
    if not p.exists():
        return None
    with closing(open_store(p)) as conn:
        return {r["source"] for r in conn.execute("SELECT source FROM source WHERE enabled = 1")}


# -- writes (used by the seed + the admin API) ---------------------------------

def upsert_source(cfg: SourceConfig, *, clear_removed: bool = False, path: str | Path | None = None) -> None:
    """Write a source's settings. A new source starts enabled (active); an existing one keeps its run state, which
    only set_state changes: a settings save that also wrote `enabled` re-enabled a delisted vendor."""
    p = _params(cfg, enabled=True)
    cols = ", ".join(p)
    placeholders = ", ".join(f":{c}" for c in p)
    updates = ", ".join(f"{c} = :{c}" for c in p if c not in ("source", "enabled"))
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
    single write; any omitted leaves it unchanged, except that switching the flag off without a lifecycle labels
    the source 'paused' (unless it is paused or delisted already): enabled 0 never goes unlabelled. The lifecycle verbs and the admission auto-demote go
    through here."""
    sets: list[str] = []
    params: list = []
    if lifecycle is not None:
        sets.append("lifecycle = ?"); params.append(lifecycle)
    if enabled is not None:
        sets.append("enabled = ?"); params.append(1 if enabled else 0)
    if enabled is False and lifecycle is None:
        # never the run flag off without a label: a source switched off is paused, unless already paused or delisted
        sets.append("lifecycle = CASE WHEN lifecycle IN ('paused', 'delisted') THEN lifecycle ELSE 'paused' END")
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


def clear_source_diagnostics(sources: list[str] | None = None, path: str | Path | None = None) -> int:
    """Drop the persisted per-source diagnostics (the funnel/listed/stages the admin's Diagnostics and
    /scrapers pages read). A GLOBAL/pristine reset calls this so the 'listed' column starts BLANK after a
    factory reset -- otherwise the last-produce values linger (the reset clears the ledger/base but not this
    display table). Each source refills its diagnostic on its next produce. sources=None clears every source;
    a list scopes it. Returns the number of rows dropped."""
    p = Path(path or config_db_path())
    if not p.exists():
        return 0
    with closing(open_store(p)) as conn:
        if sources is None:
            n = conn.execute("DELETE FROM source_diagnostic").rowcount
        else:
            n = sum(conn.execute("DELETE FROM source_diagnostic WHERE source = ?", (s,)).rowcount
                    for s in sources)
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


# stages that emit a catalog and so APPLY the operator's decisions (mirror produce._CATALOG_STAGES; kept
# inline to avoid importing the heavy produce module into this low-level store, and store is imported BY it).
_APPLIED_STAGES = ("catalog", "all", "republish")


def _last_applied_produce(conn) -> dict | None:
    """The run record of the newest SUCCESSFUL catalog/all/republish run in the bounded run_log (<=50 rows), the
    last run whose emitted catalog reflected the decisions; None when none has completed."""
    for r in conn.execute("SELECT record FROM run_log WHERE finished_at IS NOT NULL "
                          "ORDER BY finished_at DESC LIMIT 50"):
        rec = json.loads(r["record"])
        if rec.get("status") == "succeeded" and rec.get("stage") in _APPLIED_STAGES:
            return rec
    return None


def last_applied_produce_at(path: str | Path | None = None) -> str | None:
    """When the last produce that APPLIED the operator's decisions finished (see _last_applied_produce): a
    decision made after it has not reached any product yet. A pure read: never creates config.db."""
    with closing(read_store(path)) as conn:
        rec = _last_applied_produce(conn)
    return rec["finished_at"] if rec else None


def decisions_pending_apply(path: str | Path | None = None) -> dict:
    """The 're-produce to apply' signal (no product scan): how many operator decisions the emitted catalog may not
    reflect yet, because the last catalog-applying produce did not read them.

    A produce reads the decisions ONCE, at curate start, so the cut is that run's `started_at`, not its finish: a
    statement made while it ran (after its read) reached no product, and counts. Counting from the start may also
    count a statement made in the few moments between the start and the read, which is the safe side (it asks for
    one produce more, never one fewer). Counts the `statement` table (bind / mint / reject) and the operator's
    `backbone_leaf_decision` verdicts (variety membership), the two decision stores a produce consumes; a
    scrape-only or inventory run applies no decisions. The membership rows the produce itself records for a minted
    variety (decided_by 'produce') are its output, not decisions, so they never count, whatever their time.
    Statements count by DISTINCT spelling_norm: one operator mint writes a vendor row AND a global (source='') row for
    the same spelling, so counting rows would double it; a spelling is one decision. All timestamps are UTC
    `datetime.isoformat()`, so the lexicographic compare is chronological. Returns {changed, latest_decision_at,
    last_produce_at (that run's finish)}; changed counts every decision when nothing has produced yet."""
    with closing(open_store(path)) as conn:
        applied = _last_applied_produce(conn)
        since = applied["started_at"] if applied else ""      # '' precedes every timestamp: count them all
        row = conn.execute(
            "SELECT (SELECT COUNT(DISTINCT spelling_norm) FROM statement WHERE decided_at > ?) + "
            "(SELECT COUNT(*) FROM backbone_leaf_decision WHERE decided_by = 'operator' AND decided_at > ?) AS n, "
            "(SELECT MAX(d) FROM (SELECT MAX(decided_at) AS d FROM statement UNION ALL "
            "SELECT MAX(decided_at) FROM backbone_leaf_decision WHERE decided_by = 'operator')) AS latest",
            (since, since)).fetchone()
    return {"changed": row["n"], "latest_decision_at": row["latest"],
            "last_produce_at": applied["finished_at"] if applied else None}


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
            p = _params(cfg, enabled=True)
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
