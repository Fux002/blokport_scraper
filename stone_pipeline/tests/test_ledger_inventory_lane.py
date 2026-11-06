"""The inventory lane: a failed or refused stock ack waits and dead-letters by the product lane's rule (it never
re-serves on every pull), the stock includes its reason and lot, stock moves for every product Medusa holds, and
GET /status counts exactly what the lane serves."""

from __future__ import annotations

import sqlite3

from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_discontinued
from stone_pipeline.ledger.server import dispatch

PAST = "2000-01-01T00:00:00+00:00"
_NEW_COLUMNS = {"last_synced_reason", "last_synced_lot_code", "sync_attempts", "sync_error", "retry_after"}


def _product(ledger, sku, state="synced", medusa_id="M", lot_code=None):
    now = now_iso()
    ledger.execute("INSERT INTO product (sku, source, state, medusa_id, lot_code, created_at, updated_at) "
                   "VALUES (?, 's', ?, ?, ?, ?, ?)", (sku, state, medusa_id, lot_code, now, now))


def _stock(ledger, sku, qty, *, made_to_order=0, reason=None, lot=None, synced=None):
    """One stock row; `synced` (qty, made_to_order, reason, lot) is what Medusa last acked, None = never synced."""
    s = synced or (None, None, None, None)
    ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, reason, replaced_by_lot_code, last_synced_qty, "
                   "last_synced_made_to_order, last_synced_reason, last_synced_lot_code, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (sku, qty, made_to_order, reason, lot, *s, now_iso()))


def _lane(ledger):
    return [i["external_id"] for i in sync.ready_inventory(ledger)]


def _wait_has_passed(ledger, sku):
    ledger.execute("UPDATE inventory SET retry_after = ? WHERE sku = ?", (PAST, sku))


def _row(ledger, sku):
    return ledger.get("inventory", "sku", sku)


# --- defect 1: a failed or refused ack waits, then dead-letters -------------------------------------------------

def test_a_failed_stock_ack_waits_before_it_is_served_again(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4)
        assert _lane(ledger) == ["S-1"]
        assert sync.ack(ledger, "inventory", "S-1", status_="failed", reason="SKU not found") == 1
        row = _row(ledger, "S-1")
        assert (row["sync_attempts"], row["sync_error"]) == (1, "SKU not found")
        assert row["retry_after"] > now_iso()
        assert _lane(ledger) == [] and sync.count_ready(ledger, "inventory") == 0
        _wait_has_passed(ledger, "S-1")
        assert _lane(ledger) == ["S-1"]


def test_a_made_to_order_row_acked_with_quantity_0_backs_off_dead_letters_and_requeues(tmp_path):
    # Blokport develop echoes Number(quantity) || 0, so a made-to-order stock (quantity null) comes back as 0. The echo
    # guard refuses it (kept as is); the refusal now waits and dead-letters instead of re-serving on every pull.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", None, made_to_order=1)
        for attempt in range(1, sync._MAX_SYNC_ATTEMPTS + 1):
            assert _lane(ledger) == ["S-1"]
            assert sync.ack(ledger, "inventory", "S-1", status_="updated", quantity=0) == 0   # refused
            row = _row(ledger, "S-1")
            assert row["sync_attempts"] == attempt and row["retry_after"] > now_iso()
            assert row["last_synced_made_to_order"] is None                                 # nothing recorded
            assert _lane(ledger) == []                                                       # not before the wait
            _wait_has_passed(ledger, "S-1")
        assert _lane(ledger) == []                                                           # dead
        assert sync.status(ledger)["inventory"]["delta"] == 0
        dead = [f for f in sync.failures(ledger) if f["type"] == "inventory"]
        assert [(f["external_id"], f["state"], f["attempts"]) for f in dead] == [("S-1", "dead", sync._MAX_SYNC_ATTEMPTS)]
        assert "refused" in dead[0]["error"]
        assert sync.requeue_dead_lettered(ledger) == 1
        row = _row(ledger, "S-1")
        assert (row["sync_attempts"], row["sync_error"], row["retry_after"]) == (0, None, None)
        assert _lane(ledger) == ["S-1"]
        assert [f for f in sync.failures(ledger) if f["type"] == "inventory"] == []


def test_a_success_clears_the_failures(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4)
        sync.ack(ledger, "inventory", "S-1", status_="failed", reason="timeout")
        assert sync.ack(ledger, "inventory", "S-1", status_="updated", quantity=4) == 1
        row = _row(ledger, "S-1")
        assert (row["sync_attempts"], row["sync_error"], row["retry_after"], row["last_synced_qty"]) == (0, None, None, 4)


def test_a_new_stock_is_served_at_once_even_while_a_failure_waits(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4, synced=(4, 0, None, None))
        populate_discontinued(ledger, [("S-1", "h")])                 # the supplier dropped it: stock 0
        sync.ack(ledger, "inventory", "S-1", status_="failed", reason="timeout")
        assert _lane(ledger) == []
        sync.delist_source(ledger, None)                              # nothing on sale: no change, still waits
        assert _lane(ledger) == []
        populate_discontinued(ledger, [("S-1", "h")], {"S-1": "L1"})  # now replaced by its lot: a new stock
        _product(ledger, "P-1", lot_code="L1")                        # a piece of the lot is in Medusa
        assert _lane(ledger) == ["S-1"] and _row(ledger, "S-1")["sync_attempts"] == 0


def test_requeue_takes_the_inventory_type_over_http(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4)
        for _ in range(sync._MAX_SYNC_ATTEMPTS):
            sync.ack(ledger, "inventory", "S-1", status_="failed", reason="x")
        assert dispatch(ledger, "POST", "requeue", {}, {"type": "inventory"}) == (200, {"requeued": 1})
        assert _lane(ledger) == ["S-1"]
        assert dispatch(ledger, "POST", "requeue", {}, {"type": "stock"})[0] == 400


# --- defect 2: the reason and the lot are part of the stock ----------------------------------------------------

def test_a_reason_that_changes_at_stock_0_is_served_and_its_ack_records_it(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _product(ledger, "P-1", lot_code="L1")
        _stock(ledger, "S-1", 0, synced=(0, 0, None, None))           # out of stock, Medusa has it
        assert _lane(ledger) == []
        populate_discontinued(ledger, [("S-1", "h")], {"S-1": "L1"})  # now replaced by its lot, still stock 0
        assert sync.ready_inventory(ledger) == [{"external_id": "S-1", "payload": {
            "sku": "S-1", "quantity": 0, "made_to_order": False, "reason": "replaced_by_lot", "lot_code": "L1"}}]
        assert sync.ack(ledger, "inventory", "S-1", status_="updated", quantity=0) == 1
        row = _row(ledger, "S-1")
        assert (row["last_synced_reason"], row["last_synced_lot_code"]) == ("replaced_by_lot", "L1")
        assert _lane(ledger) == []
        _product(ledger, "P-2", lot_code="L2")
        populate_discontinued(ledger, [("S-1", "h")], {"S-1": "L2"})  # re-cut into another lot: a new lot code
        assert _lane(ledger) == ["S-1"]


def test_a_reason_cleared_at_stock_0_is_served(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 0, reason="no_photo", synced=(0, 0, "no_photo", None))
        assert _lane(ledger) == []
        populate_discontinued(ledger, [("S-1", "h")])                 # dropped: dead stock, no reason any more
        assert [i["payload"] for i in sync.ready_inventory(ledger)] == [
            {"sku": "S-1", "quantity": 0, "made_to_order": False}]


# --- defect 7: stock moves for every product Medusa holds -------------------------------------------------------

def test_stock_moves_for_a_product_medusa_holds_whatever_its_sync_state(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for sku, state in (("D-1", "dirty"), ("S-1", "syncing"), ("G-1", "gap_held")):
            _product(ledger, sku, state=state, medusa_id="M-" + sku)
            _stock(ledger, sku, 3)
        _product(ledger, "N-1", state="synced", medusa_id=None)       # never acked: Medusa does not hold it
        _stock(ledger, "N-1", 3)
        assert _lane(ledger) == ["D-1", "G-1", "S-1"]
        assert sync.ack(ledger, "inventory", "D-1", status_="updated", quantity=3) == 1
        assert sync.ack(ledger, "inventory", "N-1", status_="updated", quantity=3) == 0
        assert _row(ledger, "N-1")["last_synced_qty"] is None


# --- defect 8: GET /status counts what the lane serves ---------------------------------------------------------

def test_the_status_delta_is_the_lane(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "A-1")
        _stock(ledger, "A-1", 2)                                      # served
        _product(ledger, "B-1", medusa_id=None)
        _stock(ledger, "B-1", 2)                                      # not in Medusa yet
        _product(ledger, "C-1")
        _stock(ledger, "C-1", 2)
        sync.ack(ledger, "inventory", "C-1", status_="failed", reason="x")   # waiting
        _product(ledger, "D-1")
        _stock(ledger, "D-1", 0, reason="replaced_by_lot", lot="L9")  # its lot has no piece in Medusa
        assert sync.status(ledger)["inventory"] == {"total": 4, "delta": len(_lane(ledger))} == {"total": 4, "delta": 1}


# --- the v13 migration --------------------------------------------------------------------------------------------

def _v12_ledger(path) -> None:
    with Ledger.open(path, env="development") as ledger:
        _product(ledger, "S-1")
        _product(ledger, "S-2")
        _stock(ledger, "S-1", 4, synced=(4, 0, None, None))
        _stock(ledger, "S-2", 0, reason="no_photo", synced=(0, 0, None, None))
    raw = sqlite3.connect(path)
    for col in sorted(_NEW_COLUMNS):
        raw.execute(f"ALTER TABLE inventory DROP COLUMN {col}")
    raw.execute("PRAGMA user_version = 12")
    raw.execute("UPDATE ledger_meta SET schema_version = 12")
    raw.commit()
    raw.close()


def _columns(ledger) -> list[tuple]:
    return [(r["name"], r["type"], r["notnull"], r["dflt_value"]) for r in ledger.execute("PRAGMA table_info(inventory)")]


def test_a_v12_ledger_gains_the_columns_and_serves_only_rows_with_a_reason(tmp_path):
    with Ledger.open(tmp_path / "fresh.ledger", env="development") as fresh:
        fresh_columns = _columns(fresh)
    assert _NEW_COLUMNS <= {c[0] for c in fresh_columns}
    p = tmp_path / "mig.ledger"
    _v12_ledger(p)
    with Ledger.open(p, env="development") as ledger:
        assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 13
        assert _columns(ledger) == fresh_columns
        assert {r["sku"]: r["sync_attempts"] for r in ledger.execute("SELECT * FROM inventory")} == {"S-1": 0, "S-2": 0}
        assert _lane(ledger) == ["S-2"]                               # its last synced reason is not known
        ddl = ledger.execute("SELECT sql FROM sqlite_master WHERE name = 'inventory'").fetchone()[0]
    with Ledger.open(p, env="development") as ledger:                 # a re-open is a no-op
        assert ledger.execute("SELECT sql FROM sqlite_master WHERE name = 'inventory'").fetchone()[0] == ddl
        assert _columns(ledger) == fresh_columns and _lane(ledger) == ["S-2"]
