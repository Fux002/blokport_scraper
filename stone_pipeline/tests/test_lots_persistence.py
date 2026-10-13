"""Lots across the boundaries: the per-source `lots` switch in the config store (the cutover control, safe
against an admin form that does not know the field), and lot_code / lot_position through the ledger into
the pull payload Blokport reads (stored, part of the change fingerprint, null outside a lot)."""

from __future__ import annotations

import sqlite3

import pytest

from stone_pipeline.config import server, store
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products
from stone_pipeline.ledger.sync import ready
from stone_pipeline.stages import emit

KEY = "slab_marble_calacatta_viola_1"


# -- config store: the cutover switch -----------------------------------------------------------------------

def test_lots_is_off_by_default_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas Marble", "ports": ["Izmir"]})
    assert store.read_sources()["sentas"].lots is False and store.get_row("sentas")["lots"] is False
    store.upsert_row({**store.get_row("sentas"), "lots": True})
    assert store.read_sources()["sentas"].lots is True and store.get_row("sentas")["lots"] is True


def test_a_partial_put_changes_only_the_fields_it_names(tmp_path, monkeypatch):
    # the cutover is one partial PUT {"lots": true}; and an admin form that does not render a field must
    # never reset it to its default on save (that would silently undo a cutover).
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas Marble", "ports": ["Izmir"],
                      "enhance": False, "primary_origin": "TR", "min_expected_rows": 120})
    code, body = server.dispatch("PUT", ["sources", "sentas"], {"lots": True})
    assert code == 200 and body["lots"] is True
    assert (body["vendor"], body["ports"], body["enhance"], body["primary_origin"], body["min_expected_rows"]) \
        == ("Sentas Marble", ["Izmir"], False, "TR", 120)
    code, body = server.dispatch("PUT", ["sources", "sentas"], {"vendor": "Sentas Mermer"})   # a form without `lots`
    assert code == 200 and body["lots"] is True and body["vendor"] == "Sentas Mermer"


# -- ledger: stored, fingerprinted, migrated ----------------------------------------------------------------

def _row(lot_code="B6392-u", lot_position=5):
    return CanonicalRow(
        src_site="sentas", surrogate_key="7149-5", color_name="Purple", finish_name="Honed", quality_name="A",
        type_name="Marble", variation_key=KEY, variation_name="Calacatta Viola", title="t", description="d",
        handle="h", slug="h", weight=0.3, length=3.17, width=1.98, height=0.02, company_id="CO",
        sales_channel_id="SC", thumbnail_key="i.jpg", lot_code=lot_code, lot_position=lot_position)


def _seed_variation(ledger):
    now = now_iso()
    ledger.upsert("variation", {"key": KEY, "branch": "slab", "type": "Marble", "name": "Calacatta Viola",
                                "aliases": "[]", "image_url": "https://s3/tex.png", "image_sha256": None,
                                "image_model": None,
                                "volume": "", "medusa_id": "V1", "in_full": 1, "payload_hash": "",
                                "state": "synced", "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))


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
                                      "lot_code": code, "lot_position": pos,
                                      "created_at": now, "updated_at": now}, pk=("sku",))
        by = {r["external_id"]: r["payload"] for r in ready(ledger, "products")}
        assert (by["SEN-7149-5"]["lot_code"], by["SEN-7149-5"]["lot_position"]) == ("B6392-u", 5)
        assert by["SEN-2213"]["lot_code"] is None and by["SEN-2213"]["lot_position"] is None


def test_a_v6_ledger_gains_the_lot_columns_and_keeps_its_data(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v6 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _seed_variation(lg)
    raw = sqlite3.connect(p)                                                # simulate a v6 ledger
    raw.execute("DROP INDEX idx_product_lot_code")                          # a v6 ledger has neither
    for col in ("lot_code", "lot_position"):
        raw.execute(f"ALTER TABLE product DROP COLUMN {col}")
    raw.execute("ALTER TABLE inventory DROP COLUMN replaced_by_lot_code")
    raw.execute("PRAGMA user_version = 6")
    raw.execute("UPDATE ledger_meta SET schema_version = 6")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in lg.conn.execute("PRAGMA table_info(product)")}
        assert {"lot_code", "lot_position"} <= cols
        assert lg.get("variation", "key", KEY)["name"] == "Calacatta Viola"
