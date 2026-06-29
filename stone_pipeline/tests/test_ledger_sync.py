"""Sync service over the ledger: status, and the serve/ack loop that keeps the
ledger and Medusa in sync."""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import ack, ready, status


def _variation(ledger, key, state, medusa_id=None):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": "slab", "type": "", "name": "x",
                                "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": medusa_id,
                                "in_full": 1, "payload_hash": "", "state": state,
                                "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _product(ledger, sku, variation_key, state):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "s", "variation_key": variation_key,
                              "state": state, "created_at": now, "updated_at": now}, pk=("sku",))


def _seed(ledger: Ledger) -> None:
    now = now_iso()
    ledger.upsert("attribute", {"category": "color", "value": "Black", "medusa_id": "C1",
                                "state": "synced", "created_at": now, "updated_at": now},
                  pk=("category", "value"))
    for key, state in [("slab_a_1", "synced"), ("slab_b_2", "pending")]:
        ledger.upsert("variation", {"key": key, "branch": "slab", "type": "", "name": "x",
                                    "aliases": "[]", "image_url": "", "image_sha256": None,
                                    "image_model": None, "volume": "", "medusa_id": None,
                                    "in_full": 1, "payload_hash": "", "state": state,
                                    "first_seen": now, "last_synced": None,
                                    "created_at": now, "updated_at": now}, pk=("key",))
    ledger.upsert("product", {"sku": "S-1", "source": "s", "variation_key": "slab_a_1",
                              "state": "pending", "created_at": now, "updated_at": now}, pk=("sku",))
    # one stock row that moved (delta) and one already synced
    ledger.upsert("inventory", {"sku": "S-1", "qty": 5, "last_synced_qty": None,
                                "updated_at": now}, pk=("sku",))


def test_status_reports_state_histograms_and_inventory_delta(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        st = status(ledger)

    assert st["attribute"] == {"synced": 1}
    assert st["variation"] == {"synced": 1, "pending": 1}
    assert st["product"] == {"pending": 1}
    assert st["combination"] == {}              # empty until materialized
    assert st["inventory"] == {"total": 1, "delta": 1}


def test_sync_loop_serve_ack_converges(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_new_1", state="pending")          # new, no id yet
        _product(ledger, "S-1", "slab_new_1", state="pending")

        # the variation is served; the product is NOT (its variation is not synced)
        assert [r["external_id"] for r in ready(ledger, "variations")] == ["slab_new_1"]
        assert ready(ledger, "products") == []

        # Medusa applies the variation and acks the id it minted -> it stops being served
        ack(ledger, "variations", "slab_new_1", "VID-1", "created")
        assert ready(ledger, "variations") == []
        v = ledger.get("variation", "key", "slab_new_1")
        assert v["medusa_id"] == "VID-1" and v["state"] == "synced"

        # now the product is eligible (its variation is synced); apply + ack it
        assert [r["external_id"] for r in ready(ledger, "products")] == ["S-1"]
        ack(ledger, "products", "S-1", "PID-1", "created")
        assert ready(ledger, "products") == []
        p = ledger.get("product", "sku", "S-1")
        assert p["medusa_id"] == "PID-1" and p["state"] == "synced"

        # a failed ack returns it to dirty, so it is offered again next pull
        ack(ledger, "products", "S-1", None, "failed")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["S-1"]


def test_dispatch_routes_status_ready_ack(tmp_path):
    from stone_pipeline.ledger.server import dispatch

    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_new_1", state="pending")

        code, body = dispatch(ledger, "GET", "status", {}, None)
        assert code == 200 and body["variation"] == {"pending": 1}

        code, body = dispatch(ledger, "GET", "variations", {}, None)
        assert code == 200 and [i["external_id"] for i in body["items"]] == ["slab_new_1"]

        code, body = dispatch(ledger, "POST", "ack", {},
                              [{"type": "variations", "external_id": "slab_new_1",
                                "medusa_id": "VID-1", "status": "created"}])
        assert code == 200 and body["acked"] == 1
        assert ledger.get("variation", "key", "slab_new_1")["state"] == "synced"

        assert dispatch(ledger, "GET", "bogus", {}, None)[0] == 404
        assert dispatch(ledger, "POST", "ack", {}, {"not": "a list"})[0] == 400
