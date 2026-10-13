"""A bundle replaced by its lot: the deactivation itself carries the redirect target.

When a vendor's cutover splits a bundle into its pieces, the bundle's SKU vanishes from the scrape and
takes the existing discontinued path (inventory 0). Agreed with Blokport: that inventory delta carries
`reason: "replaced_by_lot"` and the `lot_code`, so Blokport deactivates the bundle (never deletes it) and
301s its slug to the lot page from the same message. The delta is served only once a piece of that lot
exists in Medusa, so the redirect never points at an empty lot. A plain sold-out stays a bare zero.
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_discontinued, populate_inventory
from stone_pipeline.ledger.sync import ready
from stone_pipeline.stages import emit, lots

CFG = SourceConfig(source="sentas", source_code="sen", lots=True)
CODE = "B6208B-1b4d1c6e-0000-5000-8000-000000000000"


def _piece(position, ref="2555", code=CODE):
    return CanonicalRow(src_site="sentas", surrogate_key=f"{ref}-{position}", src_lot_ref=ref,
                        lot_code=code, lot_position=position)


# -- which vanished SKU was replaced by which lot ----------------------------------------------------------

def test_replacements_map_each_lot_s_bundle_sku_to_its_code_and_piece_count():
    rows = [_piece(4), _piece(13), _piece(1, ref="7149", code="B6392-x"),
            CanonicalRow(src_site="sentas", surrogate_key="2213")]                # a whole bundle
    assert lots.replacements(rows, CFG) == {"SEN-2555": (CODE, 2), "SEN-7149": ("B6392-x", 1)}


# -- the ledger and the inventory lane ------------------------------------------------------------------

def _product(ledger, sku, *, lot_code=None, medusa_id=None, state="synced"):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "sen", "state": state, "lot_code": lot_code,
                              "medusa_id": medusa_id, "created_at": now, "updated_at": now}, pk=("sku",))


def _inventory_row(ledger, sku):
    return ledger.execute("SELECT * FROM inventory WHERE sku = ?", (sku,)).fetchone()


def _synced_stock(ledger, sku, qty):
    ledger.upsert("inventory", {"sku": sku, "qty": qty, "last_synced_qty": qty, "updated_at": now_iso()},
                  pk=("sku",))


def test_a_replaced_bundle_is_zeroed_with_its_lot_and_a_plain_drop_stays_a_bare_zero(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for sku in ("SEN-2555", "SEN-2213"):
            _product(ledger, sku, medusa_id=f"m-{sku}")
            _synced_stock(ledger, sku, 9)
        populate_discontinued(ledger, [("SEN-2555", "h-2555"), ("SEN-2213", "h-2213")],
                              replaced={"SEN-2555": CODE})
        replaced, plain = _inventory_row(ledger, "SEN-2555"), _inventory_row(ledger, "SEN-2213")
        assert (replaced["qty"], replaced["reason"], replaced["replaced_by_lot_code"]) == (0, "replaced_by_lot", CODE)
        assert (plain["qty"], plain["reason"], plain["replaced_by_lot_code"]) == (0, None, None)


def test_the_replacement_is_served_only_once_a_piece_of_the_lot_exists_in_medusa(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "SEN-2555", medusa_id="m-bundle")
        _synced_stock(ledger, "SEN-2555", 9)
        _product(ledger, "SEN-2555-4", lot_code=CODE, state="pending")          # piece not in Medusa yet
        populate_discontinued(ledger, [("SEN-2555", "h")], replaced={"SEN-2555": CODE})
        assert [r["external_id"] for r in ready(ledger, "inventory")] == []    # the lot page would be empty

        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'm-piece' WHERE sku = 'SEN-2555-4'")
        out = ready(ledger, "inventory")
        assert out == [{"external_id": "SEN-2555", "payload": {
            "sku": "SEN-2555", "quantity": 0, "reason": "replaced_by_lot", "lot_code": CODE}}]


def test_a_plain_sold_out_delta_carries_no_reason_and_no_lot(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "SEN-2213", medusa_id="m")
        _synced_stock(ledger, "SEN-2213", 9)
        populate_discontinued(ledger, [("SEN-2213", "h")])
        assert ready(ledger, "inventory") == [{"external_id": "SEN-2213",
                                               "payload": {"sku": "SEN-2213", "quantity": 0}}]


def test_a_bundle_listed_again_drops_its_replacement(tmp_path):
    # a reverted cutover: the bundle is scraped as one product again, so it is no longer replaced
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _product(ledger, "SEN-2555", medusa_id="m")
        _synced_stock(ledger, "SEN-2555", 9)
        populate_discontinued(ledger, [("SEN-2555", "h")], replaced={"SEN-2555": CODE})
        bundle = CanonicalRow(src_site="sentas", surrogate_key="2555", inventory_quantity=9)
        populate_inventory(ledger, [bundle], CFG)
        row = _inventory_row(ledger, "SEN-2555")
        assert (row["qty"], row["reason"], row["replaced_by_lot_code"]) == (9, None, None)


def test_a_v7_ledger_gains_the_replacement_column(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v7 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _product(lg, "SEN-1")
    raw = sqlite3.connect(p)
    raw.execute("ALTER TABLE inventory DROP COLUMN replaced_by_lot_code")
    raw.execute("DROP INDEX idx_product_lot_code")                          # v7 (dev today) has neither
    raw.execute("PRAGMA user_version = 7")
    raw.execute("UPDATE ledger_meta SET schema_version = 7")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "replaced_by_lot_code" in {r[1] for r in lg.conn.execute("PRAGMA table_info(inventory)")}
        assert lg.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_product_lot_code'").fetchone()
        assert lg.get("product", "sku", "SEN-1")["source"] == "sen"


# -- the review record both sides read before resuming the pull -----------------------------------------

def test_the_review_folder_records_every_replacement_and_never_suggests_deleting_it(tmp_path):
    discontinued = [("SEN-2555", "afyon-sugar-honed-sen-2555"), ("SEN-2213", "afyon-gold-honed-sen-2213")]
    replaced = {"SEN-2555": (CODE, 9)}
    emit.write_discontinued_csv(discontinued, tmp_path / "products_discontinued.csv",
                                replaced={sku: code for sku, (code, _) in replaced.items()})
    emit.write_lot_replacements_csv(discontinued, replaced, tmp_path / "lot_replacements.csv")
    with (tmp_path / "products_discontinued.csv").open(encoding="utf-8-sig") as h:
        actions = {r["Variant Sku"]: r["Action"] for r in csv.DictReader(h)}
    assert "delete" not in actions["SEN-2555"] and CODE in actions["SEN-2555"]
    with (tmp_path / "lot_replacements.csv").open(encoding="utf-8-sig") as h:
        assert list(csv.DictReader(h)) == [{"Variant Sku": "SEN-2555", "Product Handle": "afyon-sugar-honed-sen-2555",
                                            "Lot Code": CODE, "Pieces": "9"}]
