"""Acks are a closed set per lane and never clear a stored Medusa id; the removed lane delivers every delete (a
reconciled variety's products too), can be requeued, and backs off a refused 'done'; the payload_hash covers exactly
the served payload, and new content clears an abandoned dead-letter."""

from __future__ import annotations

import csv
import dataclasses
import sqlite3

from stone_pipeline.tests._alias_sidecar import write_alias_sidecar
import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.ledger import payloads, sync
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products, populate_variations_full
from stone_pipeline.ledger.server import dispatch
from stone_pipeline.stages import emit
from stone_pipeline.stages.emit_catalog import _COLS
from stone_pipeline.tests.test_ledger_products import _fixture_rows, _seed_ids

PAST = "2000-01-01T00:00:00+00:00"


def _variation(ledger, key, state="synced", medusa_id="V-1"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "type": "Marble", "name": "x", "alias_rows": "[]", "codes": "[]",
                                "medusa_id": medusa_id, "in_full": 1, "payload_hash": "h",
                                "state": state, "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _product(ledger, sku, variation_key="marble__x_1", state="syncing", medusa_id=None):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "s", "variation_key": variation_key, "state": state,
                              "medusa_id": medusa_id, "payload_hash": "h", "created_at": now, "updated_at": now},
                  pk=("sku",))


def _stock(ledger, sku, qty=3):
    ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, updated_at) VALUES (?, ?, 0, ?)",
                   (sku, qty, now_iso()))


def _batch(ledger, *acks):
    return sync.ack_batch(ledger, list(acks))


# --- defect 3: a closed set of statuses per lane; anything else fails loud, never clears an id ------------------------

@pytest.mark.parametrize("status", ["created", "updated", "skipped"])
def test_every_success_status_of_the_entity_lanes_applies(tmp_path, status):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1")
        _product(ledger, "S-1")
        assert _batch(ledger, {"type": "products", "external_id": "S-1", "medusa_id": "P-1", "status": status}) \
            == {"applied": 1, "missed": 0, "skipped": 0}
        assert ledger.get("product", "sku", "S-1")["state"] == "synced"


_TABLE = {"products": ("product", "sku"), "variations": ("variation", "key"), "inventory": ("inventory", "sku"),
          "removed": ("removed", "external_id")}


@pytest.mark.parametrize("type_, status", [
    ("products", "done"), ("products", "blocked"), ("products", "applied"), ("variations", "deleted"),
    ("inventory", "created"), ("inventory", "done"), ("removed", "created"), ("removed", "updated"),
])
def test_a_status_outside_its_lane_is_a_failed_attempt_never_an_apply(tmp_path, type_, status):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1", state="syncing", medusa_id="V-1")
        _product(ledger, "S-1", medusa_id="P-1")
        _stock(ledger, "S-1")
        ledger.execute("UPDATE inventory SET last_synced_qty = 5, last_synced_made_to_order = 0")   # a count move
        sync.record_tombstones(ledger, [("T-1", "s")])
        xid = {"products": "S-1", "variations": "marble__x_1", "inventory": "S-1", "removed": "T-1"}[type_]
        table, pk = _TABLE[type_]
        for attempt in range(1, SETTINGS.sync.max_attempts + 1):
            _batch(ledger, {"type": type_, "external_id": xid, "medusa_id": "M-9", "status": status})
            row = ledger.get(table, pk, xid)
            assert row["sync_attempts"] == attempt and row["sync_error"] == f"unknown ack status {status!r}"
            assert row["retry_after"] > now_iso()                             # it backs off
            if table in ("variation", "product"):
                assert row["medusa_id"] in ("V-1", "P-1")                     # never overwritten
        assert xid in [f["external_id"] for f in sync.failures(ledger) if f["type"] == type_]   # dead-lettered


@pytest.mark.parametrize("medusa_id", [None, ""])
def test_a_success_without_an_id_is_a_failed_attempt_and_keeps_the_stored_id(tmp_path, medusa_id):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1", state="syncing", medusa_id="V-1")
        ack = {"type": "variations", "external_id": "marble__x_1", "status": "updated"}
        if medusa_id is not None:
            ack["medusa_id"] = medusa_id
        for _ in range(SETTINGS.sync.max_attempts):
            _batch(ledger, ack)
        row = ledger.get("variation", "key", "marble__x_1")
        assert (row["medusa_id"], row["state"], row["sync_error"]) == ("V-1", "gap_held", "success ack without medusa_id")
        assert [f["external_id"] for f in sync.failures(ledger)] == ["marble__x_1"]
        # the stored id survives, so a later retire still tells Medusa to delete it (no local-only drop)
        assert sync.retire_variation(ledger, "marble__x_1")["tombstoned_variation"] is True


def test_an_inventory_skipped_is_a_stock_the_consumer_did_not_apply(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1")
        _product(ledger, "S-1", state="synced", medusa_id="P-1")
        _stock(ledger, "S-1", 3)
        for attempt in range(1, SETTINGS.sync.max_attempts + 1):
            _batch(ledger, {"type": "inventory", "external_id": "S-1", "status": "skipped", "quantity": 3})
            row = ledger.get("inventory", "sku", "S-1")
            assert row["last_synced_qty"] is None                              # not synced
            assert (row["sync_attempts"], row["sync_error"]) == (attempt, "not applied by the consumer (skipped)")
            assert row["retry_after"] > now_iso()
        assert [(f["type"], f["state"]) for f in sync.failures(ledger)] == [("inventory", "dead")]


def test_an_unknown_lane_is_skipped(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        assert _batch(ledger, {"type": "combinations", "external_id": "c", "status": "created"}) \
            == {"applied": 0, "missed": 0, "skipped": 1}


def test_an_inventory_and_a_removed_ack_carry_no_id_and_still_apply(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1")
        _product(ledger, "S-1", state="synced", medusa_id="P-1")
        _stock(ledger, "S-1", 3)
        sync.record_tombstones(ledger, [("T-1", "s")])
        assert _batch(ledger, {"type": "inventory", "external_id": "S-1", "status": "updated", "quantity": 3},
                      {"type": "removed", "external_id": "T-1", "status": "done"}) \
            == {"applied": 2, "missed": 0, "skipped": 0}
        assert ledger.get("inventory", "sku", "S-1")["last_synced_qty"] == 3
        assert ledger.get("removed", "external_id", "T-1") is None


def test_a_failed_tombstone_ack_counts_like_a_block(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        sync.record_tombstones(ledger, [("T-1", "s")])
        assert _batch(ledger, {"type": "removed", "external_id": "T-1", "status": "failed", "reason": "500"}) \
            == {"applied": 1, "missed": 0, "skipped": 0}
        row = ledger.get("removed", "external_id", "T-1")
        assert (row["state"], row["sync_attempts"], row["sync_error"]) == ("pending", 1, "500")


# --- defect 4: a reconciled variety's synced products are tombstoned ------------------------------------------------

def test_reconcile_tombstones_the_synced_products_of_a_dropped_variety(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__ghost_1")
        _product(ledger, "S-1", "marble__ghost_1", state="synced", medusa_id="P-1")   # Medusa holds it
        _product(ledger, "S-2", "marble__ghost_1", state="pending")                   # never sent
        _stock(ledger, "S-1")
        out = sync.reconcile_variations_to_seed(ledger, set(), seed_identities=set())
        assert out["dropped_keys"] == ["marble__ghost_1"]
        assert ledger.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 0
        assert [(i["payload"]["kind"], i["external_id"]) for i in sync.ready(ledger, "removed")] == [
            ("product", "S-1"), ("variation", "marble__ghost_1")]


def test_reconcile_tombstones_a_products_export_row_a_run_made_dirty(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__ghost_1")
        _product(ledger, "B-1", "marble__ghost_1", state="dirty")           # no id acked, but ...
        ledger.execute("UPDATE product SET last_synced = ? WHERE sku = 'B-1'", (now_iso(),))   # ... the export showed it
        sync.reconcile_variations_to_seed(ledger, set(), seed_identities=set())
        assert [(i["payload"]["kind"], i["external_id"]) for i in sync.ready(ledger, "removed")] == [
            ("product", "B-1"), ("variation", "marble__ghost_1")]


def test_reconcile_refuses_while_a_pull_holds_a_lease(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__ghost_1")
        _product(ledger, "S-1", "marble__ghost_1", state="syncing")           # leased to a live pull
        with pytest.raises(sync.ServeInFlight):
            sync.reconcile_variations_to_seed(ledger, set(), seed_identities=set())
        assert ledger.get("product", "sku", "S-1") is not None


# --- never a hard-deleted product without its tombstone ------------------------------------------------------------
# Ledger.execute never commits: the tombstone upserts and the DELETEs run in one transaction, committed by the Ledger
# context's exit and rolled back by it when anything raises. So a delete that fails rolls the tombstones back with it.

class _FailingDelete(Ledger):
    """The ledger, with the one statement starting `fail_on` raising (a disk error mid-purge)."""

    fail_on = ""

    def execute(self, sql, params=()):
        if sql.startswith(self.fail_on):
            raise sqlite3.OperationalError(f"injected failure: {self.fail_on}")
        return super().execute(sql, params)


def _interrupted(path, fail_on, work):
    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    ledger = _FailingDelete(raw, "development")
    ledger.fail_on = fail_on
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        with ledger:
            work(ledger)


def _dead_stock(path):
    with Ledger.open(path, env="development") as ledger:
        _variation(ledger, "marble__x_1")
        for sku in ("S-1", "S-2"):
            _product(ledger, sku, state="synced", medusa_id="P-" + sku)
            _stock(ledger, sku, 0)
        _product(ledger, "S-3", state="synced", medusa_id="P-S-3")
        _stock(ledger, "S-3", 4)                                              # on sale: not purged


@pytest.mark.parametrize("reason", ["discontinued", "vendor_removed"])
@pytest.mark.parametrize("fail_on", ["DELETE FROM product", "DELETE FROM inventory"])
def test_purge_never_deletes_a_product_without_its_tombstone(tmp_path, reason, fail_on):
    path = tmp_path / "l.db"
    _dead_stock(path)
    _interrupted(path, fail_on, lambda lg: sync.purge_discontinued(lg, reason=reason))
    with Ledger.open(path, env="development") as ledger:                     # (b): everything rolled back
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0
        assert sorted(r["sku"] for r in ledger.execute("SELECT sku FROM product")) == ["S-1", "S-2", "S-3"]
        assert ledger.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 3
        out = sync.purge_discontinued(ledger, reason=reason)                  # the re-run purges
    with Ledger.open(path, env="development") as ledger:
        assert sorted(out["external_ids"]) == ["S-1", "S-2"]
        tombstones = [tuple(r) for r in ledger.execute("SELECT external_id, kind, reason FROM removed ORDER BY 1")]
        assert tombstones == [("S-1", "product", reason), ("S-2", "product", reason)]   # exactly one per SKU
        assert [r["sku"] for r in ledger.execute("SELECT sku FROM product")] == ["S-3"]


@pytest.mark.parametrize("fail_on", ["DELETE FROM product", "DELETE FROM inventory", "DELETE FROM variation"])
def test_the_reconcile_never_deletes_a_product_without_its_tombstone(tmp_path, fail_on):
    path = tmp_path / "l.db"
    with Ledger.open(path, env="development") as ledger:
        _variation(ledger, "marble__ghost_1")
        _product(ledger, "S-1", "marble__ghost_1", state="synced", medusa_id="P-1")
        _stock(ledger, "S-1")
    _interrupted(path, fail_on, lambda lg: sync.reconcile_variations_to_seed(lg, set(), seed_identities=set()))
    with Ledger.open(path, env="development") as ledger:                     # rolled back whole
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0
        assert ledger.get("product", "sku", "S-1") is not None
        sync.reconcile_variations_to_seed(ledger, set(), seed_identities=set())
    with Ledger.open(path, env="development") as ledger:
        assert [tuple(r) for r in ledger.execute("SELECT external_id, kind FROM removed ORDER BY kind")] == [
            ("S-1", "product"), ("marble__ghost_1", "variation")]
        assert ledger.get("product", "sku", "S-1") is None


# --- defect 5: a dead tombstone can be requeued ----------------------------------------------------------------------

def test_a_dead_tombstone_is_requeued(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        sync.record_tombstones(ledger, [("T-1", "s"), ("T-2", "s")])
        for _ in range(SETTINGS.sync.max_attempts):
            for xid in ("T-1", "T-2"):
                sync.ack(ledger, "removed", xid, status_="blocked", reason="open reservation")
        assert {r["state"] for r in ledger.execute("SELECT state FROM removed")} == {"dead"}
        assert sync.abandon_dead_letter(ledger, "removed", "T-2")["abandoned"] is True
        assert dispatch(ledger, "POST", "requeue", {}, {"type": "removed"}) == (200, {"requeued": 1})
        row = ledger.get("removed", "external_id", "T-1")
        assert (row["state"], row["sync_attempts"], row["sync_error"], row["retry_after"]) == ("pending", 0, None, None)
        assert [i["external_id"] for i in sync.ready(ledger, "removed")] == ["T-1"]   # the abandoned one stays
        assert sync.requeue_dead_lettered(ledger) == 0                                  # nothing left dead


# --- defect 6: a refused 'done' on a variety tombstone backs off --------------------------------------------------

def test_a_done_on_a_variety_that_regained_children_waits_like_a_block(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _variation(ledger, "marble__x_1", state="retiring")
        sync.record_tombstones(ledger, [("marble__x_1", None)], reason="variation_removed", kind="variation")
        _product(ledger, "S-1", "marble__x_1")                          # a raced produce re-linked a product
        for attempt in range(1, SETTINGS.sync.max_attempts + 1):
            assert sync.ack(ledger, "removed", "marble__x_1", status_="done") == 0
            row = ledger.get("removed", "external_id", "marble__x_1")
            assert row["sync_attempts"] == attempt and row["retry_after"] > now_iso()
            assert sync.ready(ledger, "removed") == []
            ledger.execute("UPDATE removed SET retry_after = ?", (PAST,))
        assert ledger.get("removed", "external_id", "marble__x_1")["state"] == "dead"
        assert ledger.get("variation", "key", "marble__x_1") is not None


# --- defect 9: the hash is the served payload; new content clears an abandoned dead-letter --------------------------

def _variants_full(path, rows):
    with path.open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=_COLS)
        w.writeheader()
        for key, name, aliases in rows:
            w.writerow({"Key": key, "Name": name, "Type": "Marble", "Aliases": aliases})
    return write_alias_sidecar(path)


def _typed_synced_variation(ledger, path):
    now = now_iso()
    ledger.upsert("attribute", {"category": "type", "value": "Marble", "medusa_id": "T1", "state": "synced",
                                "created_at": now, "updated_at": now}, pk=("category", "value"))
    populate_variations_full(ledger, path)
    ledger.execute("UPDATE variation SET state = 'synced', medusa_id = 'V-1' WHERE in_full = 1")


def test_the_variation_hash_is_the_served_payload(tmp_path):
    key = "marble__carrara_0001"
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _typed_synced_variation(ledger, _variants_full(tmp_path / "f.csv", [(key, "Carrara", "A|B")]))
        ledger.execute("UPDATE variation SET state = 'dirty'")
        served = sync.ready(ledger, "variations")[0]
        assert served["payload_hash"] == payloads.payload_hash_of(served["payload"])
        ledger.execute("UPDATE variation SET state = 'synced'")
        populate_variations_full(ledger, _variants_full(tmp_path / "f.csv", [(key, "Carrara", "A|B")]))
        assert ledger.get("variation", "key", key)["state"] == "synced"      # unchanged content does not re-serve
        populate_variations_full(ledger, _variants_full(tmp_path / "f.csv", [(key, "Carrara", "A|C")]))
        assert ledger.get("variation", "key", key)["state"] == "dirty"       # aliases are


def _products(ledger, rows, cfg):
    populate_products(ledger, rows, cfg)
    ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'P-' || sku")


@pytest.mark.usefixtures("ledger_product_pcats")   # its rows carry PC1 / PC2 (table 577: the registry's)
def test_the_product_hash_covers_vendor_and_thumbnail_not_the_sales_channel(tmp_path):
    cfg = load_source("polonine")
    rows = _fixture_rows()
    sku = emit._sku(rows[0], cfg)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _seed_ids(ledger)
        _products(ledger, rows, cfg)
        populate_products(ledger, [rows[0].model_copy(update={"sales_channel_id": "SC-other"})] + rows[1:], cfg)
        assert ledger.get("product", "sku", sku)["state"] == "synced"        # not served
        populate_products(ledger, [rows[0].model_copy(update={"thumbnail_key": "new-thumb.jpg"})] + rows[1:], cfg)
        assert ledger.get("product", "sku", sku)["state"] == "dirty"
        _products(ledger, rows, cfg)
        populate_products(ledger, rows, dataclasses.replace(cfg, vendor="Another Company"))
        assert ledger.get("product", "sku", sku)["state"] == "dirty"


@pytest.mark.usefixtures("ledger_product_pcats")   # its rows carry PC1 / PC2 (table 577: the registry's)
def test_new_content_clears_an_abandoned_dead_letter(tmp_path):
    cfg = load_source("polonine")
    rows = _fixture_rows()
    sku = emit._sku(rows[0], cfg)
    key = "marble__carrara_0001"
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _seed_ids(ledger)
        _products(ledger, rows, cfg)
        _typed_synced_variation(ledger, _variants_full(tmp_path / "f.csv", [(key, "Carrara", "A")]))
        ledger.execute("UPDATE product SET state = 'gap_held', abandoned_at = ? WHERE sku = ?", (now_iso(), sku))
        ledger.execute("UPDATE variation SET state = 'gap_held', abandoned_at = ? WHERE key = ?", (now_iso(), key))
        populate_products(ledger, rows, cfg)                                    # unchanged: stays abandoned
        assert ledger.get("product", "sku", sku)["abandoned_at"] is not None
        populate_products(ledger, [rows[0].model_copy(update={"title": "New title"})] + rows[1:], cfg)
        populate_variations_full(ledger, _variants_full(tmp_path / "f.csv", [(key, "Carrara Renamed", "A")]))
        for table, pk, value in (("product", "sku", sku), ("variation", "key", key)):
            row = ledger.get(table, pk, value)
            assert (row["state"], row["abandoned_at"]) == ("dirty", None)
        assert [f for f in sync.failures(ledger) if f["external_id"] in (sku, key)] == []
