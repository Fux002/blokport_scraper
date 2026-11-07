"""C8 contract pin: sync.ack() split into _ack_removed / _ack_inventory / _ack_entity with ack() a thin
dispatcher. One case per (lane, status) pair of the closed status sets (_ACK_STATUSES), plus the two cross-
lane rules (unknown status, and a success without a medusa_id on an entity lane), pinning the ledger row the
ack leaves behind. These outcomes are the behaviour before the split; the split must leave them byte-identical.
"""

from __future__ import annotations

import pytest

from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import Ledger, now_iso


def _open(tmp_path):
    return Ledger.open(tmp_path / "dev.ledger", env="development")


def _variation(ledger, key, state, medusa_id=None):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": "slab", "type": "Marble", "name": "x", "aliases": "[]",
                                "image_url": "", "image_sha256": None, "image_model": None, "volume": "",
                                "medusa_id": medusa_id, "in_full": 1, "payload_hash": "", "state": state,
                                "first_seen": now, "last_synced": None, "created_at": now,
                                "updated_at": now}, pk=("key",))


def _product(ledger, sku, variation_key, state="pending", medusa_id=None):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "s", "variation_key": variation_key, "state": state,
                              "medusa_id": medusa_id, "created_at": now, "updated_at": now}, pk=("sku",))


def _stock(ledger, sku, qty):
    ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, last_synced_qty, last_synced_made_to_order, "
                   "updated_at) VALUES (?, ?, 0, NULL, NULL, ?)", (sku, qty, now_iso()))


def _removed(ledger, external_id, kind="product"):
    now = now_iso()
    ledger.upsert("removed", {"external_id": external_id, "source": "s", "reason": "vendor_removed",
                              "kind": kind, "state": "pending", "sync_attempts": 0, "sync_error": None,
                              "created_at": now, "updated_at": now}, pk=("external_id",))


# --- entity lanes: variations and products -----------------------------------------------------------------

@pytest.mark.parametrize("lane", ["variations", "products"])
@pytest.mark.parametrize("status", ["created", "updated", "skipped", "synced"])
def test_entity_success_with_id_marks_synced(tmp_path, lane, status):
    with _open(tmp_path) as ledger:
        if lane == "variations":
            _variation(ledger, "slab_a_1", state="pending")
            xid, table, pk = "slab_a_1", "variation", "key"
        else:
            _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
            _product(ledger, "P-1", "slab_a_1", state="pending")
            xid, table, pk = "P-1", "product", "sku"
        assert sync.ack(ledger, lane, xid, medusa_id="M", status_=status) == 1
        row = ledger.get(table, pk, xid)
        assert row["state"] == "synced" and row["medusa_id"] == "M"
        assert row["sync_attempts"] == 0 and row["sync_error"] is None and row["retry_after"] is None
        assert row["last_synced"] is not None


@pytest.mark.parametrize("lane", ["variations", "products"])
def test_entity_failed_reserves_dirty(tmp_path, lane):
    with _open(tmp_path) as ledger:
        if lane == "variations":
            _variation(ledger, "slab_a_1", state="syncing")
            xid, table, pk = "slab_a_1", "variation", "key"
        else:
            _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
            _product(ledger, "P-1", "slab_a_1", state="syncing")
            xid, table, pk = "P-1", "product", "sku"
        assert sync.ack(ledger, lane, xid, status_="failed", reason=None) == 1
        row = ledger.get(table, pk, xid)
        assert row["state"] == "dirty" and row["sync_attempts"] == 1
        assert row["sync_error"] == "apply failed" and row["retry_after"] is not None
        assert row["medusa_id"] is None                                   # a failed ack never writes an id


@pytest.mark.parametrize("lane", ["variations", "products"])
def test_entity_unknown_status_is_a_failed_attempt(tmp_path, lane):
    with _open(tmp_path) as ledger:
        if lane == "variations":
            _variation(ledger, "slab_a_1", state="syncing")
            xid, table, pk = "slab_a_1", "variation", "key"
        else:
            _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
            _product(ledger, "P-1", "slab_a_1", state="syncing")
            xid, table, pk = "P-1", "product", "sku"
        assert sync.ack(ledger, lane, xid, medusa_id="M", status_="weird") == 1
        row = ledger.get(table, pk, xid)
        assert row["state"] == "dirty" and row["sync_error"] == "unknown ack status 'weird'"


@pytest.mark.parametrize("lane", ["variations", "products"])
def test_entity_success_without_id_is_a_failed_attempt(tmp_path, lane):
    with _open(tmp_path) as ledger:
        if lane == "variations":
            _variation(ledger, "slab_a_1", state="syncing")
            xid, table, pk = "slab_a_1", "variation", "key"
        else:
            _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
            _product(ledger, "P-1", "slab_a_1", state="syncing")
            xid, table, pk = "P-1", "product", "sku"
        assert sync.ack(ledger, lane, xid, medusa_id=None, status_="created") == 1
        row = ledger.get(table, pk, xid)
        assert row["state"] == "dirty" and row["sync_error"] == "success ack without medusa_id"


# --- inventory ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["updated", "synced"])
def test_inventory_success_records_synced(tmp_path, status):
    with _open(tmp_path) as ledger:
        _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
        _product(ledger, "S-1", "slab_a_1", state="synced", medusa_id="M")
        _stock(ledger, "S-1", 4)
        assert sync.ack(ledger, "inventory", "S-1", status_=status, quantity=4) == 1
        row = ledger.get("inventory", "sku", "S-1")
        assert row["last_synced_qty"] == 4 and row["sync_attempts"] == 0 and row["sync_error"] is None


@pytest.mark.parametrize("status,error", [("failed", "boom"), ("skipped", "not applied by the consumer (skipped)")])
def test_inventory_failure_counts_an_attempt(tmp_path, status, error):
    with _open(tmp_path) as ledger:
        _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
        _product(ledger, "S-1", "slab_a_1", state="synced", medusa_id="M")
        _stock(ledger, "S-1", 4)
        assert sync.ack(ledger, "inventory", "S-1", status_=status, reason="boom") == 1
        row = ledger.get("inventory", "sku", "S-1")
        assert row["sync_attempts"] == 1 and row["sync_error"] == error
        assert row["retry_after"] is not None and row["last_synced_qty"] is None


def test_inventory_unknown_status_counts_an_attempt(tmp_path):
    with _open(tmp_path) as ledger:
        _variation(ledger, "slab_a_1", state="synced", medusa_id="VM")
        _product(ledger, "S-1", "slab_a_1", state="synced", medusa_id="M")
        _stock(ledger, "S-1", 4)
        assert sync.ack(ledger, "inventory", "S-1", status_="weird") == 1
        row = ledger.get("inventory", "sku", "S-1")
        assert row["sync_attempts"] == 1 and row["sync_error"] == "unknown ack status 'weird'"


# --- removed -----------------------------------------------------------------------------------------------

def test_removed_done_deletes_the_tombstone(tmp_path):
    with _open(tmp_path) as ledger:
        _removed(ledger, "R-1", kind="product")
        assert sync.ack(ledger, "removed", "R-1", status_="done") == 1
        assert ledger.get("removed", "external_id", "R-1") is None


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_removed_failure_blocks_and_waits(tmp_path, status):
    with _open(tmp_path) as ledger:
        _removed(ledger, "R-1", kind="product")
        assert sync.ack(ledger, "removed", "R-1", status_=status) == 1
        row = ledger.get("removed", "external_id", "R-1")
        assert row["state"] == "pending" and row["sync_attempts"] == 1
        assert row["sync_error"] == "blocked" and row["retry_after"] is not None


def test_removed_unknown_status_blocks_with_its_reason(tmp_path):
    with _open(tmp_path) as ledger:
        _removed(ledger, "R-1", kind="product")
        assert sync.ack(ledger, "removed", "R-1", status_="weird") == 1
        row = ledger.get("removed", "external_id", "R-1")
        assert row["state"] == "pending" and row["sync_error"] == "unknown ack status 'weird'"


def test_unknown_lane_raises(tmp_path):
    with _open(tmp_path) as ledger:
        with pytest.raises(ValueError):
            sync.ack(ledger, "nonsense", "X-1", status_="synced")
