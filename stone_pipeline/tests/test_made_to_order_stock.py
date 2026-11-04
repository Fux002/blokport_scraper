"""Made to order: a free-size shape is on sale with no count. One stock state end to end.

The row (derive) -> the CSVs (no quantity, inventory not managed) -> the ledger (inventory qty NULL,
made_to_order 1) -> the product record (made_to_order, read at create) and the inventory lane
({sku, quantity: null, made_to_order: true}) -> the ack, which echoes both and is refused when either moved.
A hidden or discontinued shape is a plain stock 0, never made to order.
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_discontinued, populate_inventory, populate_products
from stone_pipeline.ledger.render import render_inventory, render_products
from stone_pipeline.stages import emit
from stone_pipeline.stages.derive import derive_inventory
from stone_pipeline.stages.product_state import inventory_str, manage_inventory_str
from stone_pipeline.tests.test_ledger_products import _fixture_rows, _seed_ids

KEY = "tile_marble_pietra_grey_0001"


@pytest.fixture(scope="module")
def cfg():
    return load_source("polonine")


def _row(sku: str, *, made_to_order: bool, count: str = "5") -> CanonicalRow:
    r = CanonicalRow(src_site="polonine", surrogate_key=sku, variation_id="V1", handle=f"h-{sku.lower()}",
                     raw_slab_count=count, free_size_length=0.8 if made_to_order else None,
                     free_size_width=0.4 if made_to_order else None)
    derive_inventory(r)
    return r


def _seed(ledger: Ledger) -> None:
    now = now_iso()
    ledger.upsert("variation", {
        "key": KEY, "branch": "tile", "type": "Marble", "name": "Pietra Grey", "aliases": "[]",
        "image_url": f"https://bucket/variations/{KEY}.png", "image_sha256": None, "image_model": None,
        "volume": "", "medusa_id": "V1", "payload_hash": "", "state": "synced", "first_seen": now,
        "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def _synced(ledger: Ledger, rows, cfg) -> None:
    populate_products(ledger, rows, cfg)
    ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'P-' || sku, variation_key = ?", (KEY,))
    populate_inventory(ledger, rows, cfg)


def _lane(ledger: Ledger) -> dict:
    return {item["external_id"]: item["payload"] for item in sync.ready_inventory(ledger)}


# --- the row and the CSVs -------------------------------------------------------------------------------

def test_a_made_to_order_row_has_no_count_and_unmanaged_inventory_in_every_csv(cfg, tmp_path):
    mto, counted = _row("A", made_to_order=True), _row("B", made_to_order=False)
    assert (inventory_str(mto), manage_inventory_str(mto)) == ("", "false")
    assert (inventory_str(counted), manage_inventory_str(counted)) == ("5", "true")
    cells = {c: emit.COLUMN_MAP[c](mto, cfg) for c in ("Variant Inventory Quantity", "Variant Manage Inventory")}
    assert cells == {"Variant Inventory Quantity": "", "Variant Manage Inventory": "false"}


def test_the_ledger_render_of_a_made_to_order_delta_equals_emit(cfg, tmp_path):
    rows = [_row("A", made_to_order=True), _row("B", made_to_order=False)]
    direct = tmp_path / "direct.csv"
    emit.write_inventory_csv(rows, cfg, direct)
    via = tmp_path / "via.csv"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        populate_products(ledger, rows, cfg)
        populate_inventory(ledger, rows, cfg)
        render_inventory(ledger, cfg, via)
    assert via.read_bytes() == direct.read_bytes()


# --- the ledger and the two Blokport paths --------------------------------------------------------------

def test_a_made_to_order_shape_is_served_on_both_paths_with_no_quantity(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        populate_products(ledger, [_row("A", made_to_order=True), _row("B", made_to_order=False)], cfg)
        ledger.execute("UPDATE product SET variation_key = ?", (KEY,))
        products = {i["external_id"]: i["payload"] for i in sync.ready(ledger, "products")}
        assert products["POL-A"]["made_to_order"] is True
        assert products["POL-B"]["made_to_order"] is False
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'P-' || sku")
        populate_inventory(ledger, [_row("A", made_to_order=True), _row("B", made_to_order=False)], cfg)
        stored = {r["sku"]: (r["qty"], r["made_to_order"]) for r in ledger.execute("SELECT * FROM inventory")}
        assert stored == {"POL-A": (None, 1), "POL-B": (5, 0)}
        assert _lane(ledger) == {"POL-A": {"sku": "POL-A", "quantity": None, "made_to_order": True},
                                 "POL-B": {"sku": "POL-B", "quantity": 5, "made_to_order": False}}
        assert sync.status(ledger)["inventory"] == {"total": 2, "delta": 2}


def test_an_ack_echoing_what_was_served_clears_the_delta(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=True), _row("B", made_to_order=False)], cfg)
        assert sync.ack(ledger, "inventory", "POL-A", None, "synced", quantity=None, made_to_order=True) == 1
        assert sync.ack(ledger, "inventory", "POL-B", None, "synced", quantity=5, made_to_order=False) == 1
        assert _lane(ledger) == {}
        populate_inventory(ledger, [_row("A", made_to_order=True), _row("B", made_to_order=False)], cfg)
        assert _lane(ledger) == {}                         # an unchanged re-run serves nothing


def test_an_ack_for_a_state_that_moved_since_the_pull_is_refused_and_the_new_state_re_serves(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=True)], cfg)
        populate_inventory(ledger, [_row("A", made_to_order=False, count="7")], cfg)   # moved before the ack
        assert sync.ack(ledger, "inventory", "POL-A", None, "synced", quantity=None, made_to_order=True) == 0
        assert _lane(ledger) == {"POL-A": {"sku": "POL-A", "quantity": 7, "made_to_order": False}}


def test_a_shape_moving_between_made_to_order_and_a_count_is_a_delta_both_ways(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=False, count="3")], cfg)
        sync.ack(ledger, "inventory", "POL-A", None, "synced", quantity=3, made_to_order=False)
        populate_inventory(ledger, [_row("A", made_to_order=True)], cfg)
        assert _lane(ledger)["POL-A"] == {"sku": "POL-A", "quantity": None, "made_to_order": True}
        sync.ack(ledger, "inventory", "POL-A", None, "synced", quantity=None, made_to_order=True)
        populate_inventory(ledger, [_row("A", made_to_order=False, count="3")], cfg)
        assert _lane(ledger)["POL-A"] == {"sku": "POL-A", "quantity": 3, "made_to_order": False}


def test_a_hidden_or_discontinued_shape_is_a_plain_stock_zero(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=True), _row("B", made_to_order=True)], cfg)
        ledger.execute("UPDATE product SET photo_ready = 0 WHERE sku = 'POL-A'")
        populate_inventory(ledger, [_row("A", made_to_order=True)], cfg)
        populate_discontinued(ledger, [("POL-B", "h-b")])
        lane = _lane(ledger)
        assert lane["POL-A"] == {"sku": "POL-A", "quantity": 0, "made_to_order": False, "reason": "no_photo"}
        assert lane["POL-B"] == {"sku": "POL-B", "quantity": 0, "made_to_order": False}


def test_a_soft_reset_re_serves_a_made_to_order_shape(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=True)], cfg)
        sync.ack(ledger, "inventory", "POL-A", None, "synced", quantity=None, made_to_order=True)
        sync.reset_sync_state(ledger)
        ledger.execute("UPDATE product SET state = 'synced'")
        assert "POL-A" in _lane(ledger)


def test_a_made_to_order_row_must_carry_no_count(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        ledger.execute("INSERT INTO product (sku, source, state, created_at, updated_at) "
                       "VALUES ('POL-A', 'pol', 'synced', 'now', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, updated_at) VALUES ('POL-A', 4, 1, 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, updated_at) "
                           "VALUES ('POL-A', NULL, 0, 'now')")


# --- the v12 migration ----------------------------------------------------------------------------------

def _v11_ledger(path) -> None:
    with Ledger.open(path, env="development") as ledger:
        now = now_iso()
        for sku in ("POL-1", "POL-2"):
            ledger.execute("INSERT INTO product (sku, source, state, created_at, updated_at) "
                           "VALUES (?, 'pol', 'synced', ?, ?)", (sku, now, now))
    raw = sqlite3.connect(path)
    raw.executescript("""
        DROP TABLE inventory;
        CREATE TABLE inventory (sku TEXT PRIMARY KEY REFERENCES product (sku), qty INTEGER NOT NULL,
            last_synced_qty INTEGER, reason TEXT, replaced_by_lot_code TEXT, updated_at TEXT NOT NULL);
        CREATE INDEX idx_inventory_delta ON inventory (last_synced_qty, qty);
        INSERT INTO inventory (sku, qty, last_synced_qty, reason, updated_at) VALUES ('POL-1', 4, 4, NULL, 'u');
        INSERT INTO inventory (sku, qty, last_synced_qty, reason, updated_at) VALUES ('POL-2', 0, NULL, 'no_photo', 'u');
    """)
    raw.execute("ALTER TABLE product DROP COLUMN made_to_order")
    raw.execute("PRAGMA user_version = 11")
    raw.execute("UPDATE ledger_meta SET schema_version = 11")
    raw.commit()
    raw.close()


def test_a_v11_ledger_keeps_its_stock_and_sync_marks_and_serves_nothing_new(tmp_path):
    p = tmp_path / "mig.ledger"
    _v11_ledger(p)
    with Ledger.open(p, env="development") as ledger:
        assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = {r["sku"]: dict(r) for r in ledger.execute("SELECT * FROM inventory")}
        assert {k: (v["qty"], v["last_synced_qty"], v["made_to_order"], v["last_synced_made_to_order"], v["reason"])
                for k, v in rows.items()} == {"POL-1": (4, 4, 0, 0, None), "POL-2": (0, None, 0, None, "no_photo")}
        assert sync.status(ledger)["inventory"] == {"total": 2, "delta": 1}     # only the never-synced one
        assert {r["made_to_order"] for r in ledger.execute("SELECT made_to_order FROM product")} == {0}


def test_a_made_to_order_shape_is_live_and_a_vendor_take_offline_delists_it(cfg, tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        _synced(ledger, [_row("A", made_to_order=True)], cfg)
        assert sync.live_products(ledger, ["pol"]) == 1
        assert sync.variation_live_products(ledger, KEY) == 1      # the variety cannot retire under it
        assert sync.delist_source(ledger, ["pol"])["external_ids"] == ["POL-A"]
        assert _lane(ledger)["POL-A"] == {"sku": "POL-A", "quantity": 0, "made_to_order": False,
                                          "reason": "delisted"}
        assert sync.live_products(ledger, ["pol"]) == 0


def test_the_ledger_render_of_a_made_to_order_product_equals_emit(cfg, tmp_path):
    rows = _fixture_rows()
    rows[0].made_to_order = True
    columns = emit.read_template_columns()
    direct = tmp_path / "direct.csv"
    emit.write_import_csv(rows, cfg, direct, columns)
    via = tmp_path / "via.csv"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_ids(ledger)
        populate_products(ledger, rows, cfg)
        render_products(ledger, cfg, via, columns)
    assert via.read_bytes() == direct.read_bytes()
    first = next(csv.DictReader(direct.open(encoding="utf-8-sig")))  # the made-to-order row
    assert (first["Variant Manage Inventory"], first["Variant Inventory Quantity"]) == ("false", "")


def test_a_rendered_made_to_order_product_carries_no_count(cfg, tmp_path):
    from stone_pipeline.ledger.render import _product_shim
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        populate_products(ledger, [_row("A", made_to_order=True)], cfg)
        shim = _product_shim(ledger, ledger.get("product", "sku", "POL-A"))
        assert (shim.made_to_order, shim.inventory_quantity) == (True, None)


def test_a_bundle_redirects_to_a_lot_whose_piece_on_sale_is_made_to_order(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        for sku, lot in (("POL-B", None), ("POL-P", "L1")):
            ledger.execute("INSERT INTO product (sku, source, state, medusa_id, lot_code, created_at, updated_at) "
                           "VALUES (?, 'pol', 'synced', 'm', ?, ?, ?)", (sku, lot, now, now))
        ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, reason, replaced_by_lot_code, updated_at) "
                       "VALUES ('POL-B', 0, 0, 'replaced_by_lot', 'L1', ?)", (now,))
        ledger.execute("INSERT INTO inventory (sku, qty, made_to_order, last_synced_qty, last_synced_made_to_order, "
                       "updated_at) VALUES ('POL-P', NULL, 1, NULL, 1, ?)", (now,))
        assert list(_lane(ledger)) == ["POL-B"]
        assert sync.purge_discontinued(ledger)["external_ids"] == ["POL-B"]   # the made-to-order piece is kept


def test_the_migrated_inventory_table_is_exactly_the_fresh_one(tmp_path):
    p = tmp_path / "mig.ledger"
    _v11_ledger(p)
    with Ledger.open(p, env="development") as migrated, Ledger.open(tmp_path / "fresh.ledger", env="development") as fresh:
        def ddl(ledger):
            return ledger.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'inventory'").fetchone()[0]
        assert ddl(migrated) == ddl(fresh)
