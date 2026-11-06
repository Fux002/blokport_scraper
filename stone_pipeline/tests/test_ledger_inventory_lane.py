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
_NEW_COLUMNS = {"last_synced_reason", "last_synced_lot_code", "sync_attempts", "sync_error", "retry_after",
                "abandoned_at"}


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


def test_the_inventory_csv_render_sticks_to_count_changes(tmp_path):
    # 4_inventory_update.csv carries SKU + quantity only, so a reason or lot change at an unchanged count is no row
    from stone_pipeline.config.sources import load_source
    from stone_pipeline.ledger.render import render_inventory
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "POL-1", lot_code=None)
        _product(ledger, "POL-2")
        _stock(ledger, "POL-1", 0, reason="replaced_by_lot", lot="L1", synced=(0, 0, None, None))   # reason only
        _stock(ledger, "POL-2", 3, synced=(5, 0, None, None))                                        # a count move
        assert render_inventory(ledger, load_source("polonine"), tmp_path / "inv.csv") == 1


# --- defect 7: stock moves for every product Medusa holds -------------------------------------------------------

def test_stock_moves_for_a_product_medusa_holds_whatever_its_sync_state(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for sku, state in (("D-1", "dirty"), ("S-1", "syncing"), ("G-1", "gap_held")):
            _product(ledger, sku, state=state, medusa_id="M-" + sku)
            _stock(ledger, sku, 3, synced=(5, 0, None, None))             # a count move of a listed product
        _product(ledger, "E-1", state="pending", medusa_id="")            # an empty id is no id: never sent
        _stock(ledger, "E-1", 3)
        assert _lane(ledger) == ["D-1", "G-1", "S-1"]
        assert sync.ack(ledger, "inventory", "D-1", status_="updated", quantity=3) == 1
        assert sync.ack(ledger, "inventory", "E-1", status_="updated", quantity=3) == 0
        assert _row(ledger, "E-1")["last_synced_qty"] is None


def _bootstrap(ledger, tmp_path, sku, qty):
    """A product only the products export knows (bootstrap.seed_products): Medusa holds it by SKU, no id acked."""
    from stone_pipeline.ledger.bootstrap import seed_products
    export = tmp_path / "products_export.csv"
    export.write_text(f"Variant Sku,Product Handle,Variant Inventory Quantity\n{sku},h-{sku.lower()},{qty}\n",
                      encoding="utf-8")
    assert seed_products(ledger, export) == 1


def test_a_bootstrap_row_is_in_medusa_even_once_a_run_makes_it_dirty(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _bootstrap(ledger, tmp_path, "B-1", 3)
        _product(ledger, "N-1", state="pending", medusa_id=None)            # never sent: not in Medusa
        _stock(ledger, "N-1", 0, reason="delisted")
        ledger.execute("UPDATE product SET state = 'dirty' WHERE sku = 'B-1'")   # its first real run enriched it
        ledger.execute("UPDATE inventory SET qty = 0, reason = 'delisted' WHERE sku = 'B-1'")   # taken offline
        assert [i["payload"] for i in sync.ready_inventory(ledger)] == [
            {"sku": "B-1", "quantity": 0, "made_to_order": False, "reason": "delisted"}]   # the hide flows
        assert sync.ack(ledger, "inventory", "B-1", status_="updated", quantity=0) == 1
        from stone_pipeline.ledger.populate import withdraw_unsent
        from stone_pipeline.config.sources import SourceConfig
        assert withdraw_unsent(ledger, [], SourceConfig(source="b", source_code="b")) == 0   # not withdrawn
        assert withdraw_unsent(ledger, [], SourceConfig(source="s", source_code="s")) == 1   # the unsent one is
        sync.reset_sync_state(ledger, hard=True)
        assert [r["external_id"] for r in ledger.execute("SELECT external_id FROM removed")] == ["B-1"]


def test_a_dropped_product_the_export_knows_gets_its_delist(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        populate_discontinued(ledger, [("B-2", "h")])                    # a minimal row: Medusa holds it by SKU
        assert ledger.get("product", "sku", "B-2")["last_synced"] is not None
        assert [i["payload"] for i in sync.ready_inventory(ledger)] == [
            {"sku": "B-2", "quantity": 0, "made_to_order": False}]


def test_an_empty_id_without_a_sync_is_not_in_medusa_for_a_hard_reset(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "E-1", state="synced", medusa_id="")
        sync.reset_sync_state(ledger, hard=True)
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0


def test_a_lot_redirect_waits_for_a_piece_medusa_holds(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "C-B")
        _stock(ledger, "C-B", 0, reason="replaced_by_lot", lot="LOT2", synced=(2, 0, None, None))
        _product(ledger, "C-P2", state="pending", medusa_id="", lot_code="LOT2")   # an empty id: not in Medusa yet
        assert _lane(ledger) == []
        populate_discontinued(ledger, [("C-P3", "h")])                    # a piece Medusa holds by SKU only
        ledger.execute("UPDATE product SET lot_code = 'LOT2' WHERE sku = 'C-P3'")
        ledger.execute("UPDATE inventory SET qty = 1, last_synced_qty = 1, last_synced_made_to_order = 0 "
                       "WHERE sku = 'C-P3'")
        assert _lane(ledger) == ["C-B"]


# --- an un-hide never outruns the product update ---------------------------------------------------------------

def test_an_un_hide_waits_for_the_product_update_and_a_hide_does_not(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        # its new photo (photo_ready 1) is still to reach Blokport, and that apply failed: it waits
        _product(ledger, "U-1", state="dirty", medusa_id="M-U")
        ledger.execute("UPDATE product SET retry_after = '2999-01-01T00:00:00+00:00' WHERE sku = 'U-1'")
        _stock(ledger, "U-1", 5, synced=(0, 0, "no_photo", None))       # was hidden, now back on sale
        _product(ledger, "H-1", state="dirty", medusa_id="M-H")
        _stock(ledger, "H-1", 0, reason="delisted", synced=(5, 0, None, None))   # a hide
        _product(ledger, "C-1", state="dirty", medusa_id="M-C")
        _stock(ledger, "C-1", 7, synced=(5, 0, None, None))              # a count move while on sale
        _product(ledger, "N-1", state="syncing", medusa_id="M-N")
        _stock(ledger, "N-1", 2)                                          # never synced: its first stock is a show
        assert _lane(ledger) == ["C-1", "H-1"]
        assert sync.status(ledger)["inventory"]["delta"] == 2
        assert sync.status(ledger)["inventory"]["waiting"] == 2                           # U-1 and N-1
        ledger.execute("UPDATE product SET state = 'synced', retry_after = NULL")          # the update landed
        assert _lane(ledger) == ["C-1", "H-1", "N-1", "U-1"]
        assert sync.status(ledger)["inventory"]["waiting"] == 0


def test_waiting_counts_only_un_hides_the_product_gate_holds(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for sku in ("W-1", "R-1", "D-1", "A-1"):
            _product(ledger, sku, state="dirty", medusa_id="M-" + sku)
            _stock(ledger, sku, 5, synced=(0, 0, "no_photo", None))      # all un-hides of a dirty product
        ledger.execute("UPDATE inventory SET retry_after = '2999-01-01T00:00:00+00:00' WHERE sku = 'R-1'")
        ledger.execute("UPDATE inventory SET sync_attempts = ? WHERE sku IN ('D-1', 'A-1')", (sync._MAX_SYNC_ATTEMPTS,))
        ledger.execute("UPDATE inventory SET abandoned_at = '2026-01-01T00:00:00+00:00' WHERE sku = 'A-1'")
        inv = sync.status(ledger)["inventory"]
        assert (inv["waiting"], inv["dead"]) == (1, 1)                   # W-1 only; D-1 is dead, not also waiting


def test_an_un_hide_acked_after_its_product_went_dirty_is_recorded(tmp_path):
    # the re-review's repro: served while the product was synced, the product went dirty before the ack, then the
    # stock went back to hidden. The ack must be recorded, or the hide would match the old last-synced stock and
    # never be sent: Medusa would keep the product on sale.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "A-1")
        _stock(ledger, "A-1", 5, synced=(0, 0, "no_photo", None))       # was hidden, now on sale
        assert _lane(ledger) == ["A-1"]                                    # Medusa applies qty 5
        ledger.execute("UPDATE product SET state = 'dirty' WHERE sku = 'A-1'")   # a produce changed the product
        assert sync.ack(ledger, "inventory", "A-1", status_="updated", quantity=5, made_to_order=False) == 1
        ledger.execute("UPDATE inventory SET qty = 0, reason = 'no_photo' WHERE sku = 'A-1'")   # hidden again
        ledger.execute("UPDATE product SET state = 'synced' WHERE sku = 'A-1'")
        assert [i["payload"] for i in sync.ready_inventory(ledger)] == [
            {"sku": "A-1", "quantity": 0, "made_to_order": False, "reason": "no_photo"}]
        assert sync.status(ledger)["inventory"]["delta"] == 1


# --- a late or duplicate failure on a synced stock row does not count ----------------------------------------

def test_failed_acks_on_a_stock_row_already_synced_do_not_count(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4, synced=(4, 0, None, None))
        for _ in range(sync._MAX_SYNC_ATTEMPTS):
            assert sync.ack(ledger, "inventory", "S-1", status_="failed", reason="late") == 0
        row = _row(ledger, "S-1")
        assert (row["sync_attempts"], row["sync_error"], row["retry_after"]) == (0, None, None)
        assert sync.failures(ledger) == []


# --- dead stock is visible and can be abandoned --------------------------------------------------------------

def test_dead_stock_is_counted_in_status_and_can_be_abandoned(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4)
        for _ in range(sync._MAX_SYNC_ATTEMPTS):
            sync.ack(ledger, "inventory", "S-1", status_="failed", reason="SKU not found")
        assert sync.status(ledger)["inventory"] == {"total": 1, "delta": 0, "dead": 1, "waiting": 0}
        assert dispatch(ledger, "POST", "abandon", {}, {"type": "inventory", "external_id": "S-1"})[0] == 200
        assert [(f["type"], f["state"], f["abandoned"]) for f in sync.failures(ledger)] == [("inventory", "abandoned", True)]
        assert sync.requeue_dead_lettered(ledger) == 0                     # an abandoned one stays put
        assert sync.status(ledger)["inventory"]["dead"] == 0               # like gap_held: abandoned is out
        populate_discontinued(ledger, [("S-1", "h")])                     # a new stock: served again
        assert _lane(ledger) == ["S-1"] and sync.failures(ledger) == []


def test_abandon_refuses_a_live_stock_row(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "S-1")
        _stock(ledger, "S-1", 4)
        assert dispatch(ledger, "POST", "abandon", {}, {"type": "inventory", "external_id": "S-1"})[0] == 409


# --- defect 8: GET /status counts what the lane serves ---------------------------------------------------------

def test_the_status_delta_is_the_lane(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "A-1")
        _stock(ledger, "A-1", 2)                                      # served
        _product(ledger, "B-1", state="pending", medusa_id=None)
        _stock(ledger, "B-1", 2)                                      # not in Medusa yet
        _product(ledger, "C-1")
        _stock(ledger, "C-1", 2)
        sync.ack(ledger, "inventory", "C-1", status_="failed", reason="x")   # waiting
        _product(ledger, "D-1")
        _stock(ledger, "D-1", 0, reason="replaced_by_lot", lot="L9")  # its lot has no piece in Medusa
        assert sync.status(ledger)["inventory"] == {"total": 4, "delta": len(_lane(ledger)), "dead": 0, "waiting": 0} \
            == {"total": 4, "delta": 1, "dead": 0, "waiting": 0}


# --- the v13 migration --------------------------------------------------------------------------------------------

def _v12_ledger(path) -> None:
    with Ledger.open(path, env="development") as ledger:
        _product(ledger, "S-1")
        _product(ledger, "S-2")
        _stock(ledger, "S-1", 4, synced=(4, 0, None, None))
        _stock(ledger, "S-2", 0, reason="no_photo", synced=(0, 0, None, None))
        _product(ledger, "B-9", state="synced", medusa_id=None)          # a v12 products export row: no last_synced
        _product(ledger, "P-9", state="pending", medusa_id=None)         # never sent
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
        assert ledger.get("product", "sku", "B-9")["last_synced"] is not None   # Medusa holds the export row
        assert ledger.get("product", "sku", "P-9")["last_synced"] is None       # never sent
        ddl = ledger.execute("SELECT sql FROM sqlite_master WHERE name = 'inventory'").fetchone()[0]
    with Ledger.open(p, env="development") as ledger:                 # a re-open is a no-op
        assert ledger.execute("SELECT sql FROM sqlite_master WHERE name = 'inventory'").fetchone()[0] == ddl
        assert _columns(ledger) == fresh_columns and _lane(ledger) == ["S-2"]
