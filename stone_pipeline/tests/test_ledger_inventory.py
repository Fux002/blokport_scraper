"""The ledger's inventory write-through: populate seeds every product's stock, and keeps the last synced stock."""

from __future__ import annotations

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_inventory, populate_products
from stone_pipeline.stages import emit


def _seed_variations(ledger: Ledger) -> None:
    now = now_iso()
    for key, mid in [("slab_marble_a_0001", "V1"), ("block_marble_b_0002", "V2")]:
        ledger.upsert("variation", {
            "key": key, "branch": key.split("_", 1)[0], "type": "Marble", "name": "X",
            "aliases": "[]", "image_url": "", "image_sha256": None, "image_model": None,
            "volume": "", "medusa_id": mid, "payload_hash": "", "state": "synced",
            "first_seen": now, "last_synced": now, "created_at": now, "updated_at": now,
        }, pk=("key",))


def _rows() -> list[CanonicalRow]:
    from stone_pipeline.stages.derive import derive_inventory
    def row(sk, vid, handle, qty):
        r = CanonicalRow(src_site="polonine", surrogate_key=sk, variation_id=vid,
                         handle=handle, raw_slab_count=qty)
        derive_inventory(r)   # Stage 6 sets inventory_quantity before the ledger reads it
        return r
    # two with stock, one discontinued (qty 0)
    return [row("AAA", "V1", "h-aaa", "5"), row("BBB", "V2", "h-bbb", "10"),
            row("CCC", "V2", "h-ccc", "0")]


def test_initial_load_seeds_inventory_without_a_baseline(tmp_path):
    # regression: a first / baseline-less load (no products_export, so the old delta-only
    # `changed` feed was empty) must STILL carry stock. record_source now seeds inventory
    # from the FULL emit set, so every synced product serves its current stock as an
    # initial delta -- otherwise Medusa loads every product at qty 0 (the bug this fixes).
    from stone_pipeline.ledger.sync import ready
    cfg = load_source("polonine")
    rows = _rows()[:2]   # AAA qty 5, BBB qty 10 -- no prior baseline, nothing "changed"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variations(ledger)
        populate_products(ledger, rows, cfg)
        populate_inventory(ledger, rows, cfg)          # the FULL set, not a precomputed delta
        for r in rows:                                  # stock serves only for products Medusa holds
            ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'M' WHERE sku = ?", (emit._sku(r, cfg),))
        served = {i["external_id"]: i["payload"]["quantity"] for i in ready(ledger, "inventory")}
    assert served[emit._sku(rows[0], cfg)] == 5
    assert served[emit._sku(rows[1], cfg)] == 10


def test_reinventory_preserves_last_synced_qty(tmp_path):
    # regression: a re-record after an ack must NOT reset last_synced_qty to NULL,
    # or the product re-serves as a delta on every run forever.
    cfg = load_source("polonine")
    r = CanonicalRow(src_site="polonine", surrogate_key="AAA", variation_id="V1",
                     handle="h", raw_slab_count="5", inventory_quantity=5)
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variations(ledger)               # provides V1
        populate_products(ledger, [r], cfg)
        populate_inventory(ledger, [r], cfg)   # first record -> last_synced_qty NULL (a delta)
        sku = emit._sku(r, cfg)
        ledger.execute("UPDATE inventory SET last_synced_qty = 5, last_synced_made_to_order = 0 WHERE sku = ?",
                       (sku,))  # ack
        populate_inventory(ledger, [r], cfg)   # next run, same stock
        inv = ledger.get("inventory", "sku", sku)
        assert inv["last_synced_qty"] == 5, "re-record clobbered the ack-owned last_synced_qty"
        assert inv["qty"] == 5
