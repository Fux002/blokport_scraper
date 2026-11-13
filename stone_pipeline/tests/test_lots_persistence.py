"""Lots across the boundaries: no per-source setting gates them, and lot_code / lot_position travel through
the ledger into the pull payload Blokport reads (stored, part of the change fingerprint, null outside a lot)."""

from __future__ import annotations

from stone_pipeline.config import store
from stone_pipeline.config.control import server
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products
from stone_pipeline.ledger.sync import ready
from stone_pipeline.tests._ledger_icons import add_icon
from stone_pipeline.stages import emit

KEY = "marble__calacatta_viola_1"


# -- config store: no lots setting --------------------------------------------------------------------------

def test_a_partial_put_changes_only_the_fields_it_names(tmp_path, monkeypatch):
    # an admin form that does not render a field must never reset it to its default on save
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas Marble", "ports": ["Izmir"],
                      "image_pipeline": False, "primary_origin": "TR", "min_expected_rows": 120})
    code, body = server.dispatch("PUT", ["sources", "sentas"], {"vendor": "Sentas Mermer"})
    assert code == 200
    assert (body["vendor"], body["ports"], body["image_pipeline"], body["primary_origin"], body["min_expected_rows"]) \
        == ("Sentas Mermer", ["Izmir"], False, "TR", 120)


# -- ledger: stored, fingerprinted, migrated ----------------------------------------------------------------

def _row(lot_code="B6392-u", lot_position=5):
    return CanonicalRow(
        src_site="sentas", surrogate_key="7149-5", color_name="Purple", finish_name="Honed", quality_name="A",
        type_name="Marble", variation_key=KEY, variation_name="Calacatta Viola", title="t", description="d",
        handle="h", slug="h", weight=0.3, length=3.17, width=1.98, height=0.02, company_id="CO",
        sales_channel_id="SC", thumbnail_key="i.jpg", lot_code=lot_code, lot_position=lot_position)


def _seed_variation(ledger):
    now = now_iso()
    ledger.upsert("variation", {"key": KEY, "type": "Marble", "name": "Calacatta Viola",
                                "alias_rows": "[]", "codes": "[]",
                                "medusa_id": "V1", "in_full": 1, "payload_hash": "",
                                "state": "synced", "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))
    add_icon(ledger, KEY)


def test_the_ledger_stores_the_lot_and_a_lot_change_re_serves_the_piece(tmp_path):
    cfg = load_source("sentas")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        populate_products(ledger, [_row()], cfg)
        sku = emit._sku(_row(), cfg)
        stored = ledger.get("product", "sku", sku)
        assert (stored["lot_code"], stored["lot_position"]) == ("B6392-u", 5)
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'prod_1' WHERE sku = ?", (sku,))
        populate_products(ledger, [_row(lot_position=6)], cfg)
        assert ledger.get("product", "sku", sku)["state"] == "dirty"       # part of the change fingerprint


def test_the_pull_payload_carries_the_lot_and_null_outside_one(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        now = now_iso()
        for sku, code, pos in [("SEN-7149-5", "B6392-u", 5), ("SEN-2213", None, None)]:
            ledger.upsert("product", {"sku": sku, "source": "sen", "variation_key": KEY, "state": "pending",
                                      "category": "slabs", "lot_code": code, "lot_position": pos,
                                      "created_at": now, "updated_at": now}, pk=("sku",))
        by = {r["external_id"]: r["payload"] for r in ready(ledger, "products")}
        assert (by["SEN-7149-5"]["lot_code"], by["SEN-7149-5"]["lot_position"]) == ("B6392-u", 5)
        assert by["SEN-2213"]["lot_code"] is None and by["SEN-2213"]["lot_position"] is None
