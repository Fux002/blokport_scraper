"""Table 575 (G5), the SC half: a stale live export never resurrects a variety the scraper is deleting or Medusa has
deleted. Emit's export union (emit_catalog.build) skips a Key with a variation tombstone (any state) or an acked delete
(ledger.variation_deleted, written by a 'done' ack); a Key from the base or curate's delta (a real re-mint) still
enters, and populate's E5 clears its tombstone and its deleted row. Paired with AA builder's BE half (per-item
done|blocked acks on a removed batch); R4 reviews both.

Red form (R4, 11-12): every behaviour case runs the base's REAL path and fails on its assert; names the fix adds are
read with a default (getattr), and the new table is created by _deleted_table where a case reads it, so nothing
crashes on the base; every control (marked CONTROL) passes on the base. No em dashes."""
from __future__ import annotations

import csv
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from stone_pipeline.config.settings import ENV_NAME, SETTINGS
from stone_pipeline.ledger import db, populate, sync
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.stages import emit_catalog

KEPT = new_key("Marble", "Carrara")            # in the base: always emitted
GONE = new_key("Marble", "Gone Grey")           # the variety the scraper deletes
FRESH = new_key("Marble", "Fresh White")        # in the live export only, never tombstoned
REVIEW_LIST = getattr(emit_catalog, "EXPORT_KEYS_TOMBSTONED", "export_keys_tombstoned.csv")
PHASE1_VERSION = getattr(db, "PHASE1_VERSION", 15)
SCHEMA_VERSION_WITH_THE_TABLE = 20


# --- helpers -----------------------------------------------------------------------------------------------------
def _ledger(path: Path) -> Ledger:
    return Ledger.open(path, env=ENV_NAME)


def _deleted_table(ledger) -> None:
    """The fix's table, so a case that reads it reaches its assert on a ledger without it (schema.sql's DDL)."""
    ledger.execute("CREATE TABLE IF NOT EXISTS variation_deleted (key TEXT PRIMARY KEY, reason TEXT, "
                   "deleted_at TEXT NOT NULL)")


def _deleted(ledger, key):
    return ledger.execute("SELECT key, reason FROM variation_deleted WHERE key = ?", (key,)).fetchone()


def _variation(ledger, key, state="synced", medusa_id="VM-1"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "type": "Marble", "name": key, "aliases": "[]", "medusa_id": medusa_id,
                                "in_full": 1, "payload_hash": "", "state": state, "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def _write(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([emit_catalog._COLS, *rows])
    return path


def _row(key: str) -> list[str]:
    return [key, {KEPT: "Carrara", GONE: "Gone Grey", FRESH: "Fresh White"}[key], "Marble", ""]


def _emit(tmp_path, monkeypatch, *, base, export, delta=(), ledger_on=True):
    """One emit over `base` + the live `export` + curate's `delta` (lists of Keys), with the ledger at tmp. Returns
    (the emitted Keys, the review list's rows, the emitted file)."""
    review = tmp_path / "review"
    paths = dataclasses.replace(SETTINGS.paths, review_dir=review,
                                variants_export_csv=_write(tmp_path / "variants_export.csv", [_row(k) for k in export]),
                                curate_delta_csv=_write(tmp_path / "curate_delta.csv", [_row(k) for k in delta]))
    monkeypatch.setattr(emit_catalog, "SETTINGS", dataclasses.replace(SETTINGS, paths=paths))
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1" if ledger_on else "0")
    monkeypatch.setenv("SCRAPER_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    out = tmp_path / "out" / "1_variants_full.csv"
    out.parent.mkdir(exist_ok=True)
    emit_catalog.build(_write(tmp_path / "base.csv", [_row(k) for k in base]), icons={}, out_path=out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        emitted = [r["Key"] for r in csv.DictReader(h)]
    listed_path = review / REVIEW_LIST
    listed = list(csv.DictReader(listed_path.open(encoding="utf-8-sig"))) if listed_path.exists() else []
    return emitted, listed, out


def _tombstone(ledger, key, reason="not_in_seed"):
    sync.record_tombstones(ledger, [(key, None)], reason=reason, kind="variation")


def _retired(path: Path, key: str, reason: str = "variation_removed") -> None:
    """A synced variety retired: its row held 'retiring' and its variation tombstone pending."""
    with _ledger(path) as ledger:
        _variation(ledger, key)
        sync.retire_variation(ledger, key, reason=reason)


# --- R1: a pending tombstone holds against a stale export ---------------------------------------------------------
def test_r1_a_pending_tombstoned_key_only_in_the_live_export_is_not_emitted_and_keeps_its_tombstone(
        tmp_path, monkeypatch):
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _tombstone(ledger, GONE)                         # reconcile dropped it: the row is gone, the tombstone pending
    emitted, listed, out = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT]
    assert [(r["Key"], r["tombstone"], r["reason"]) for r in listed] == [(GONE, "pending", "not_in_seed")]
    with _ledger(tmp_path / "dev.ledger") as ledger:
        populate.populate_variations_full(ledger, out)
        assert ledger.get("removed", "external_id", GONE)["state"] == "pending"


# --- R2: an acked delete is remembered, and holds against a stale export ------------------------------------------
def test_r2_after_a_done_ack_a_stale_export_never_re_adds_the_variety_and_the_delete_is_recorded(
        tmp_path, monkeypatch):
    _retired(tmp_path / "dev.ledger", GONE)
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _deleted_table(ledger)
        assert sync.ack(ledger, "removed", GONE, status_="done") == 1
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT]
    assert [(r["Key"], r["tombstone"], r["reason"]) for r in listed] == [(GONE, "done", "variation_removed")]
    with _ledger(tmp_path / "dev.ledger") as ledger:
        row = _deleted(ledger, GONE)
        assert row is not None and row["reason"] == "variation_removed"


def test_control_a_product_done_ack_records_no_variety_delete(tmp_path):
    # CONTROL: passes on the base and with the fix
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _deleted_table(ledger)
        sync.record_tombstones(ledger, [("P-1", "s")], reason="discontinued", kind="product")
        assert sync.ack(ledger, "removed", "P-1", status_="done") == 1
        assert ledger.execute("SELECT COUNT(*) AS n FROM variation_deleted").fetchone()["n"] == 0


# --- R3: a real re-mint (base or curate's delta) enters, and lifts both memories -----------------------------------
@pytest.mark.parametrize("source", ["delta", "base"])
def test_r3_a_key_from_the_delta_or_base_reenters_and_clears_its_tombstone_and_deleted_row(
        tmp_path, monkeypatch, source):
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _deleted_table(ledger)
        _tombstone(ledger, GONE)
        ledger.execute("INSERT INTO variation_deleted (key, reason, deleted_at) VALUES (?, ?, ?)",
                       (GONE, "not_in_seed", now_iso()))
    base, delta = ([KEPT], [GONE]) if source == "delta" else ([KEPT, GONE], [])
    emitted, listed, out = _emit(tmp_path, monkeypatch, base=base, export=[KEPT, GONE], delta=delta)
    assert sorted(emitted) == sorted([KEPT, GONE]) and listed == []
    with _ledger(tmp_path / "dev.ledger") as ledger:
        populate.populate_variations_full(ledger, out)
        assert ledger.get("removed", "external_id", GONE) is None
        assert _deleted(ledger, GONE) is None


# --- R4: the safety net stays (CONTROLS) --------------------------------------------------------------------------
def test_r4_control_an_export_only_key_with_no_tombstone_is_still_unioned(tmp_path, monkeypatch):
    _ledger(tmp_path / "dev.ledger").close()
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, FRESH])
    assert emitted == [KEPT, FRESH] and listed == []


def test_control_without_write_through_the_union_is_unchanged(tmp_path, monkeypatch):
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, FRESH], ledger_on=False)
    assert emitted == [KEPT, FRESH] and listed == []


def test_control_before_the_first_write_creates_the_ledger_nothing_is_skipped_and_none_is_created(
        tmp_path, monkeypatch):
    # a first produce: record_catalog creates the ledger after this build, so no tombstone can exist yet
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, FRESH])
    assert emitted == [KEPT, FRESH] and listed == []
    assert not (tmp_path / "dev.ledger").exists()


def test_a_ledger_that_cannot_be_opened_fails_loud(tmp_path, monkeypatch):
    (tmp_path / "dev.ledger").write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, FRESH])


# --- R5: the paired contract, a mixed removed batch as the BE acks it (done|blocked, no reason) ----------------------
def test_r5_a_mixed_removed_batch_deletes_and_records_the_done_items_and_holds_the_blocked(tmp_path):
    other = new_key("Marble", "Other Grey")
    for key in (GONE, other):
        _retired(tmp_path / "dev.ledger", key)
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _deleted_table(ledger)
        sync.record_tombstones(ledger, [("P-1", "s"), ("P-2", "s")], reason="discontinued", kind="product")
        acks = [(GONE, "done"), (other, "blocked"), ("P-1", "done"), ("P-2", "blocked")]
        assert [sync.ack(ledger, "removed", xid, status_=status) for xid, status in acks] == [1, 1, 1, 1]
        assert ledger.get("removed", "external_id", GONE) is None
        assert ledger.get("removed", "external_id", "P-1") is None
        for xid in (other, "P-2"):
            row = ledger.get("removed", "external_id", xid)
            assert (row["state"], row["sync_attempts"]) == ("pending", 1) and row["retry_after"] is not None
        assert ledger.get("variation", "key", other)["state"] == "retiring"
        assert _deleted(ledger, other) is None
        assert _deleted(ledger, GONE) is not None


# --- R7: a Key the BE acks `blocked` stays skipped, through dead and an operator abandon ----------------------------
def test_r7_a_blocked_variation_tombstone_stays_pending_and_its_key_is_skipped(tmp_path, monkeypatch):
    _retired(tmp_path / "dev.ledger", GONE)
    with _ledger(tmp_path / "dev.ledger") as ledger:
        assert sync.ack(ledger, "removed", GONE, status_="blocked") == 1
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT]
    assert [(r["Key"], r["tombstone"]) for r in listed] == [(GONE, "pending")]


def test_r7_holds_through_dead_and_abandon_and_populate_keeps_the_dead_tombstone(tmp_path, monkeypatch):
    # the manager's findings (3) and (1): a blocked tombstone goes 'dead' on the max_attempts-th block and may then be
    # abandoned; populate's E5 deletes a tombstone in ANY state once its Key is produced
    _retired(tmp_path / "dev.ledger", GONE)
    with _ledger(tmp_path / "dev.ledger") as ledger:
        for _ in range(SETTINGS.sync.max_attempts):
            sync.ack(ledger, "removed", GONE, status_="blocked")
        assert ledger.get("removed", "external_id", GONE)["state"] == "dead"
        sync.abandon_dead_letter(ledger, "removed", GONE)
    emitted, listed, out = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT] and [(r["Key"], r["tombstone"]) for r in listed] == [(GONE, "dead")]
    with _ledger(tmp_path / "dev.ledger") as ledger:
        populate.populate_variations_full(ledger, out)
        assert ledger.get("removed", "external_id", GONE)["state"] == "dead"


def test_an_unminted_variety_is_skipped_by_its_tombstone_and_after_its_done_ack(tmp_path, monkeypatch):
    # the manager's finding (2): unmint tombstones with variation_unminted and never writes the retired memory
    # (lifecycle.py:672 calls sync.retire_variation; only retire calls add_retired)
    _retired(tmp_path / "dev.ledger", GONE, reason="variation_unminted")
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT] and [(r["tombstone"], r["reason"]) for r in listed] == [("pending", "variation_unminted")]
    with _ledger(tmp_path / "dev.ledger") as ledger:
        _deleted_table(ledger)
        assert sync.ack(ledger, "removed", GONE, status_="done") == 1
    emitted, listed, _ = _emit(tmp_path, monkeypatch, base=[KEPT], export=[KEPT, GONE])
    assert emitted == [KEPT] and [(r["tombstone"], r["reason"]) for r in listed] == [("done", "variation_unminted")]


# --- R6: the ledger version and its in-place migration ------------------------------------------------------------
def test_r6_a_phase1_ledger_gains_variation_deleted_in_place(tmp_path):
    path = tmp_path / "dev.ledger"
    with _ledger(path) as ledger:
        _variation(ledger, KEPT)
    conn = sqlite3.connect(path)                       # a ledger written before the table existed, at phase 1's version
    conn.execute("DROP TABLE IF EXISTS variation_deleted")
    conn.execute(f"PRAGMA user_version = {PHASE1_VERSION}")
    conn.execute("UPDATE ledger_meta SET schema_version = ?", (PHASE1_VERSION,))
    conn.commit()
    conn.close()
    with _ledger(path) as ledger:
        assert ledger.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION_WITH_THE_TABLE
        assert ledger.execute("SELECT name FROM sqlite_master WHERE name = 'variation_deleted'").fetchone() is not None
        assert ledger.get("variation", "key", KEPT) is not None


def test_control_a_ledger_from_before_phase1_is_still_refused(tmp_path):
    path = tmp_path / "dev.ledger"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {PHASE1_VERSION - 1}")
    conn.commit()
    conn.close()
    with pytest.raises(db.LedgerError, match="predates phase 1"):
        _ledger(path)
