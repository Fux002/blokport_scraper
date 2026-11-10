"""Phase 1 SC-3: the store writer. It applies the pure core's decisions (phase1.migrate_stores) to LOCAL copies of
the step-1 stores and writes the phase 1 stores, with no S3 and no network (plan SC-3-SC-4-PLAN.md sections 1 and 1c).

- The ledger: a NEW v15 file from the repo's own schema.sql (the one DDL source). One variation row per new Key
  (the survivor's copy, dirty), its icons from the icon copy plan, products on the new Key with their category as
  its HANDLE (D11), variation tombstones dropped and listed, every other table carried over row for row.
- config.db: retired and protected Keys moved to the new Key, review_pending cleared (the first produce refills it),
  stamped 12. Its tables are otherwise unchanged by phase 1.
- The learned-alias write-back: every record on the survivor's id.
- The icons: each flat `{old Key}.png` planned onto its pair `{new Key}/{category}.png`, add-only.
- The best-model marker: its old Keys become pair ids.

Every builder returns its report (counts, listed items, aborts) and writes nothing when anything aborts. The same
inputs give the same rows in the same order, so a re-run's logical digest is the same. No argparse, no em dashes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.categories import category
from stone_pipeline.config.store import SCHEMA_VERSION as CONFIG_SCHEMA_VERSION
from stone_pipeline.io.variety_icons import VARIATIONS_PREFIX, icon_key, icon_pair, pair_id
from stone_pipeline.ledger.db import _SCHEMA_PATH, SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from stone_pipeline.ledger.payloads import variation_hash
from stone_pipeline.phase1.migrate_stores import (KeyMap, drop_variation_tombstones, plan_variations, remap_products,
                                                  remap_protected, remap_retired, remap_writeback)

# --- config ---------------------------------------------------------------------------------------------------------
# ledger tables carried over row for row: their rows hold no variety Key (attribute values, stock by SKU, photos by
# hash, gaps by name, the audit)
CARRIED_TABLES = ("attribute", "inventory", "image", "gap", "sync_run")
FLAT_ICON_SUFFIX = ".png"                     # the old layout: {ENV_SEGMENT}/variations/{old Key}.png


@dataclass
class Report:
    """counts: what was written, by name; listed: items the operator reads (dropped tombstones, ...); aborts: kind ->
    items that stop the migration. Nothing is written when `aborts` is not empty."""
    counts: dict = field(default_factory=dict)
    listed: dict = field(default_factory=dict)
    aborts: dict = field(default_factory=dict)

    def abort(self, kind: str, items) -> None:
        if items:
            self.aborts.setdefault(kind, []).extend(sorted(items))

    def merge_aborts(self, aborts: dict) -> None:
        for kind, items in aborts.items():
            self.abort(kind, items)


# --- digests --------------------------------------------------------------------------------------------------------

def sqlite_digest(path: Path) -> str:
    """The LOGICAL content of a SQLite store: its user_version and every table's rows, sorted, as one sha256. Two
    files with the same rows give the same digest whatever their page layout (a same-content re-upload passes)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        h.update(f"user_version={conn.execute('PRAGMA user_version').fetchone()[0]}\n".encode())
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            cur = conn.execute(f'SELECT * FROM "{table}"')
            cols = [d[0] for d in cur.description]
            rows = sorted(json.dumps(dict(zip(cols, r)), sort_keys=True, default=str) for r in cur)
            h.update(f"table={table} cols={cols} n={len(rows)}\n".encode())
            for row in rows:
                h.update(row.encode() + b"\n")
        return h.hexdigest()
    finally:
        conn.close()


def bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- sqlite helpers -------------------------------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, table: str, order: str) -> list[dict]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
        return []
    cur = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    """Insert rows into a v15 table, by the v15 table's own columns (a dropped column is not carried)."""
    cols = _columns(conn, table)
    present = [c for c in cols if rows and c in rows[0]]
    if not rows:
        return 0
    conn.executemany(f'INSERT INTO "{table}" ({", ".join(present)}) VALUES ({", ".join("?" * len(present))})',
                     [[r.get(c) for c in present] for r in rows])
    return len(rows)


# --- the icons ------------------------------------------------------------------------------------------------------

@dataclass
class IconPlan:
    """copies: (source key, destination key, ETag) to copy; pairs: new Key -> {category name: ETag}, every pair the
    store will hold (copied or already there); report: counts, listed and aborts."""
    copies: list = field(default_factory=list)
    pairs: dict = field(default_factory=dict)
    report: Report = field(default_factory=Report)


def _flat_old_key(s3_key: str) -> str | None:
    """The old Key of a flat icon `{prefix}{old Key}.png`, or None for any other object."""
    if not (s3_key.startswith(VARIATIONS_PREFIX) and s3_key.endswith(FLAT_ICON_SUFFIX)):
        return None
    name = s3_key[len(VARIATIONS_PREFIX):-len(FLAT_ICON_SUFFIX)]
    return name if name and "/" not in name else None


def _old_key_category(old_key: str) -> str | None:
    """The pack category NAME an old Key was a copy of: its prefix (the old Key form `{category}_...`, read here
    only, in the migration)."""
    head = old_key.split("_", 1)[0]
    cat = category(head)
    return cat.name if cat is not None and cat.name == head else None


def plan_icon_copies(listing: dict[str, str], key_map: KeyMap) -> IconPlan:
    """Every flat `{old Key}.png` planned onto `{new Key}/{category}.png` (plan section 1c). `listing` is every object
    under the prefix: S3 key -> ETag. Add-only: a destination already there with the source's ETag is unchanged, one
    with another ETag aborts; two old icons on one pair abort, both listed. A flat icon whose old Key is not in the map
    (or has no category prefix) is listed and not copied."""
    plan = IconPlan()
    existing = {}
    for s3_key, etag in listing.items():
        pair = icon_pair(s3_key)
        if pair is not None:
            existing[s3_key] = etag
    by_destination: dict[str, list[tuple[str, str]]] = {}
    not_in_map = []
    for s3_key in sorted(listing):
        old = _flat_old_key(s3_key)
        if old is None:
            continue
        new, name = key_map.new_key(old), _old_key_category(old)
        if new is None or name is None:
            not_in_map.append(old)
            continue
        by_destination.setdefault(icon_key(new, name), []).append((s3_key, listing[s3_key]))
        plan.pairs.setdefault(new, {})[name] = listing[s3_key]
    collisions, conflicts, unchanged = [], [], 0
    for destination, sources in sorted(by_destination.items()):
        if len(sources) > 1:
            collisions += [src for src, _ in sources]
            continue
        (source, etag), = sources
        if destination in existing:
            if existing[destination] == etag:
                unchanged += 1
            else:
                conflicts.append(destination)
            continue
        plan.copies.append((source, destination, etag))
    for s3_key, etag in sorted(existing.items()):     # a pair already there and not re-copied stays a pair
        key, cat = icon_pair(s3_key)
        plan.pairs.setdefault(key, {}).setdefault(cat.name, etag)
    plan.report.abort("icon_pair_collision", collisions)
    plan.report.abort("icon_etag_conflict", conflicts)
    plan.report.listed["icon_not_in_map"] = sorted(not_in_map)
    plan.report.counts.update(icon_copies=len(plan.copies), icon_unchanged=unchanged,
                              icon_pairs=sum(len(v) for v in plan.pairs.values()))
    return plan


def remap_marker(old_keys: list[str], key_map: KeyMap) -> tuple[list[str], Report]:
    """The best-model marker's old Keys as sorted pair ids `{new Key}/{category}`; an old Key outside the map, or
    with no category prefix, aborts."""
    report, out, bad = Report(), set(), []
    for old in old_keys:
        new, name = key_map.new_key(old), _old_key_category(old)
        if new is None or name is None:
            bad.append(old)
            continue
        out.add(pair_id(new, name))
    report.abort("marker_unmapped_key", bad)
    report.counts["marker_pairs"] = len(out)
    return sorted(out), report


# --- the ledger -----------------------------------------------------------------------------------------------------

def _category_handle(label: str | None) -> str | None:
    if not label:
        return None
    cat = category(label)
    return cat.handle if cat is not None else None


def build_ledger(old_path: Path, new_path: Path, key_map: KeyMap, survivors: dict[str, str],
                 archived_keys: set[str], icon_pairs: dict[str, dict[str, str]]) -> Report:
    """The v15 ledger at `new_path` from the step-1 copy at `old_path` (module rules). `archived_keys`: the new Keys
    the new export marks archived (I3); `icon_pairs`: the icon plan's pairs. Writes nothing on an abort."""
    report = Report()
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    try:
        if _rows(old, "combination", "rowid"):
            report.abort("combination_not_empty", ["combination"])
        plan = plan_variations(_rows(old, "variation", "key"), key_map, survivors)
        report.merge_aborts(plan.aborts)
        products, aborts = remap_products(_rows(old, "product", "sku"), key_map)
        report.merge_aborts(aborts)
        unknown = sorted({p["category"] for p in products if p.get("category") and not _category_handle(p["category"])})
        report.abort("unknown_category", unknown)
        report.abort("product_without_variety",
                     [p["sku"] for p in products if p.get("variation_key") and p["variation_key"] not in plan.rows])
        kept_removed, dropped = drop_variation_tombstones(_rows(old, "removed", "external_id"), key_map)
        meta = _rows(old, "ledger_meta", "id")
        carried = {t: _rows(old, t, "rowid") for t in CARRIED_TABLES}
    finally:
        old.close()
    report.listed["dropped_variation_tombstones"] = dropped
    if report.aborts:
        return report
    new_path.unlink(missing_ok=True)
    new = sqlite3.connect(str(new_path))
    try:
        new.execute("PRAGMA foreign_keys = ON")
        new.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        new.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
        _insert(new, "ledger_meta", [{**m, "schema_version": LEDGER_SCHEMA_VERSION} for m in meta])
        for table in ("attribute",):
            report.counts[table] = _insert(new, table, carried[table])
        variations, icons = [], []
        for key, row in sorted(plan.rows.items()):
            pairs = [{"category_handle": category(name).handle, "s3_key": icon_key(key, name), "etag": etag}
                     for name, etag in sorted(icon_pairs.get(key, {}).items())]
            row = {**row, "type": key_map.type_name(plan.collapsed[key][0]), "archived": int(key in archived_keys),
                   "sync_error": None, "retry_after": None}
            row["payload_hash"] = variation_hash(row, pairs)
            variations.append(row)
            icons += [{"key": key, **p} for p in pairs]
        report.counts["variation"] = _insert(new, "variation", variations)
        report.counts["variation_category_image"] = _insert(new, "variation_category_image", icons)
        report.counts["product"] = _insert(new, "product", [{**p, "category": _category_handle(p.get("category"))}
                                                            for p in products])
        for table in ("inventory", "image", "gap", "sync_run"):
            report.counts[table] = _insert(new, table, carried[table])
        report.counts["removed"] = _insert(new, "removed", kept_removed)
        report.counts["variation_by_state"] = {s: sum(1 for v in variations if v["state"] == s)
                                               for s in sorted({v["state"] for v in variations})}
        new.commit()
    finally:
        new.close()
    return report


# --- config.db ------------------------------------------------------------------------------------------------------

def migrate_config(old_path: Path, new_path: Path, key_map: KeyMap, ledger_old_keys: set[str]) -> Report:
    """config.db at `new_path`: the step-1 copy with retired and protected Keys on their new Key, review_pending
    cleared, stamped 12. `ledger_old_keys`: the old ledger's variation Keys (a retirement is whole only when every
    copy known in this env is retired). Writes nothing on an abort."""
    report = Report()
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    try:
        retired_rows = {r["key"]: r for r in _rows(old, "retired_variation", "key")}
        protected_rows = {r["key"]: r for r in _rows(old, "protected_variation", "key")}
        pending = len(_rows(old, "review_pending", "kind, ref"))
    finally:
        old.close()
    copies = ledger_old_keys | set(retired_rows) | set(protected_rows)
    retired, aborts = remap_retired(set(retired_rows), key_map, copies)
    report.merge_aborts(aborts)
    protected, aborts = remap_protected(set(protected_rows), key_map)
    report.merge_aborts(aborts)
    if report.aborts:
        return report
    def first_at(rows: dict, col: str, new: str) -> str:   # the earliest decision among the variety's copies
        return min(r[col] for k, r in rows.items() if key_map.new_key(k) == new)

    new_path.unlink(missing_ok=True)
    shutil.copyfile(old_path, new_path)
    conn = sqlite3.connect(str(new_path))
    try:
        conn.execute("DELETE FROM retired_variation")
        conn.executemany("INSERT INTO retired_variation (key, created_at) VALUES (?, ?)",
                         [(k, first_at(retired_rows, "created_at", k)) for k in sorted(retired)])
        conn.execute("DELETE FROM protected_variation")
        conn.executemany("INSERT INTO protected_variation (key, decided_at) VALUES (?, ?)",
                         [(k, first_at(protected_rows, "decided_at", k)) for k in sorted(protected)])
        conn.execute("DELETE FROM review_pending")
        conn.execute(f"PRAGMA user_version = {CONFIG_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    report.counts.update(retired_variation=len(retired), protected_variation=len(protected))
    report.listed["review_pending_cleared"] = pending
    return report


# --- the learned-alias write-back -----------------------------------------------------------------------------------

def migrate_writeback(records: list[tuple[str, str, str]], ledger_old_rows: list[dict], key_map: KeyMap,
                      survivors: dict[str, str]) -> tuple[list[tuple[str, str, str]], Report]:
    """The write-back's records on the survivor's id (migrate_stores.remap_writeback), with its report."""
    report = Report()
    out, aborts = remap_writeback(records, ledger_old_rows, key_map, survivors)
    report.merge_aborts(aborts)
    report.counts["alias_writeback"] = len(out)
    return out, report


def ledger_variation_rows(path: Path) -> list[dict]:
    """The old ledger's variation rows (key, medusa_id), for the write-back's id map and config's copy set."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [{"key": r[0], "medusa_id": r[1]} for r in conn.execute("SELECT key, medusa_id FROM variation")]
    finally:
        conn.close()
