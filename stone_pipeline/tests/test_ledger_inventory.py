"""Inventory equivalence: populate the ledger from canonical rows, render the
stock delta, and assert it reproduces emit.write_inventory_csv byte-for-byte.

A discontinued product (qty 0) renders the same as any other served row, so the
ledger render covers both the changed-stock rows and the reversible delist that
emit writes via its `discontinued` parameter.
"""

from __future__ import annotations

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import (
    populate_discontinued,
    populate_inventory,
    populate_products,
)
from stone_pipeline.ledger.render import render_inventory
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
    def row(sk, vid, handle, qty):
        return CanonicalRow(src_site="polonine", surrogate_key=sk, variation_id=vid,
                            handle=handle, raw_slab_count=qty)
    # two with stock, one discontinued (qty 0)
    return [row("AAA", "V1", "h-aaa", "5"), row("BBB", "V2", "h-bbb", "10"),
            row("CCC", "V2", "h-ccc", "0")]


def test_inventory_populate_render_equals_emit(tmp_path):
    cfg = load_source("polonine")
    rows = _rows()
    changed, discontinued = rows[:2], rows[2:]  # two in stock, one dropped

    # emit reference: the changed rows, then the dropped one as a 0-stock delist
    sku3 = emit._sku(discontinued[0], cfg)
    direct = tmp_path / "A.csv"
    emit.write_inventory_csv(changed, cfg, direct,
                             discontinued=((sku3, discontinued[0].handle),))

    via_ledger = tmp_path / "B.csv"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variations(ledger)
        populate_products(ledger, rows, cfg)          # all exist in Medusa
        populate_inventory(ledger, changed, cfg)      # stock moved
        populate_discontinued(ledger, [(sku3, discontinued[0].handle)])  # dropped -> qty 0
        render_inventory(ledger, cfg, via_ledger)

    assert via_ledger.read_bytes() == direct.read_bytes(), (
        "inventory populate+render is NOT byte-identical to emit"
    )


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
        for r in rows:                                  # stock serves only for synced products
            ledger.set_state("product", "sku", emit._sku(r, cfg), "synced")
        served = {i["external_id"]: i["payload"]["quantity"] for i in ready(ledger, "inventory")}
    assert served[emit._sku(rows[0], cfg)] == 5
    assert served[emit._sku(rows[1], cfg)] == 10


def test_reinventory_preserves_last_synced_qty(tmp_path):
    # regression: a re-record after an ack must NOT reset last_synced_qty to NULL,
    # or the product re-serves as a delta on every run forever.
    cfg = load_source("polonine")
    r = CanonicalRow(src_site="polonine", surrogate_key="AAA", variation_id="V1",
                     handle="h", raw_slab_count="5")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variations(ledger)               # provides V1
        populate_products(ledger, [r], cfg)
        populate_inventory(ledger, [r], cfg)   # first record -> last_synced_qty NULL (a delta)
        sku = emit._sku(r, cfg)
        ledger.execute("UPDATE inventory SET last_synced_qty = 5 WHERE sku = ?", (sku,))  # ack
        populate_inventory(ledger, [r], cfg)   # next run, same stock
        inv = ledger.get("inventory", "sku", sku)
        assert inv["last_synced_qty"] == 5, "re-record clobbered the ack-owned last_synced_qty"
        assert inv["qty"] == 5
