"""Sync service status (GET /sync/status logic) over the ledger."""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import status


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
