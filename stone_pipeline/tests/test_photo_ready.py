"""The product payload carries the photo fact (owner 2026-11-03): Blokport publishes a new priced product straight from the
catalog sync, before any stock delta can tell it the product has no standardised photo, so the product itself says
`photo_ready`. It is the no-photo rule's one decision, stored on the product row: photo_ready false exactly when the
product's stock goes out with reason 'no_photo'. A change of it re-serves the product (it is in the payload hash), and a
stock-only refresh, which cannot tell whether a product has a photo, never changes it nor the reason."""
from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from stone_pipeline.config.settings import ENV_NAME, SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import populate, writethrough
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.sync import ready

KEY = "slab_marble_carrara_0001"
SKU = "POL-AAA"


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    path = tmp_path / "dev.ledger"
    with _open(path) as ledger:                     # a live variety, so its products are served
        now = now_iso()
        ledger.upsert("variation", {
            "key": KEY, "branch": "slab", "type": "Marble", "name": "Carrara", "aliases": json.dumps([]),
            "image_url": f"https://bucket/variations/{KEY}.png", "volume": "", "medusa_id": "V1",
            "payload_hash": "", "state": "synced", "first_seen": now, "last_synced": now,
            "created_at": now, "updated_at": now}, pk=("key",))
    return path


def _open(path):
    return Ledger.open(path, env=ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint())


def _rule(monkeypatch, on):
    monkeypatch.setattr(populate, "SETTINGS", dataclasses.replace(
        SETTINGS, images=dataclasses.replace(SETTINGS.images, require_standard_photo=on)))


def _row(standard_photo, qty="5"):
    return CanonicalRow(src_site="polonine", surrogate_key="AAA", variation_key=KEY, handle="h-aaa", title="Carrara",
                        inventory_quantity=qty, thumbnail_key="products/AAA/thumb.webp",
                        product_image_keys=["products/AAA/1.webp"], standard_photo=standard_photo)


def _record(path, rows, *, inventory_only=False):
    assert writethrough.record_source(rows, [], load_source("polonine"), inventory_only=inventory_only, path=path)


def _acked(path):
    """Blokport applied the product and its stock."""
    with _open(path) as ledger:
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'prod_a' WHERE sku = ?", (SKU,))
        ledger.execute("UPDATE inventory SET last_synced_qty = qty WHERE sku = ?", (SKU,))


def _state(path):
    """(served photo_ready or None when not served, inventory reason, product state, payload_hash)."""
    with _open(path) as ledger:
        product = dict(ledger.get("product", "sku", SKU))
        reason = ledger.get("inventory", "sku", SKU)["reason"]
        served = {r["external_id"]: r for r in ready(ledger, "products")}
        ledger.conn.rollback()                      # a peek: the serve's lease is not kept
    item = served.get(SKU)
    if item:
        assert item["payload_hash"] == product["payload_hash"]   # the ack's lost-update guard compares these
    return (item["payload"]["photo_ready"] if item else None), reason, product["state"], product["payload_hash"]


def test_without_a_standardised_photo_the_product_is_not_photo_ready_and_its_stock_is_no_photo(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    photo_ready, reason, _, _ = _state(ledger_path)
    assert (photo_ready, reason) == (False, "no_photo")


def test_with_a_standardised_photo_the_product_is_photo_ready(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(True)])
    photo_ready, reason, _, _ = _state(ledger_path)
    assert (photo_ready, reason) == (True, None)


def test_with_the_rule_off_every_product_is_photo_ready(ledger_path, monkeypatch):
    _rule(monkeypatch, False)
    _record(ledger_path, [_row(False)])
    photo_ready, reason, _, _ = _state(ledger_path)
    assert (photo_ready, reason) == (True, None)


def test_a_published_photo_re_serves_the_product_photo_ready(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    _, _, state, hidden_hash = _state(ledger_path)
    assert state == "synced"
    _record(ledger_path, [_row(True)])
    photo_ready, reason, state, shown_hash = _state(ledger_path)
    assert (photo_ready, reason, state) == (True, None, "dirty") and shown_hash != hidden_hash
    _acked(ledger_path)
    _record(ledger_path, [_row(False)])             # its photo taken off again
    photo_ready, reason, state, again = _state(ledger_path)
    assert (photo_ready, reason, state, again) == (False, "no_photo", "dirty", hidden_hash)


def test_a_run_that_cannot_tell_keeps_the_photo_fact(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    _record(ledger_path, [_row(None)])              # the photo pool unread this run
    photo_ready, reason, state, _ = _state(ledger_path)
    assert (photo_ready, reason, state) == (None, "no_photo", "synced")   # unchanged, so not served again
    with _open(ledger_path) as ledger:
        assert ledger.get("product", "sku", SKU)["photo_ready"] == 0


def test_a_stock_only_refresh_never_changes_the_photo_fact_or_the_reason(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    with _open(ledger_path) as ledger:
        before = dict(ledger.get("product", "sku", SKU))
    for on in (True, False):                        # even a rule switched off waits for the full run
        _rule(monkeypatch, on)
        _record(ledger_path, [_row(None, qty="9")], inventory_only=True)
        with _open(ledger_path) as ledger:
            assert dict(ledger.get("product", "sku", SKU)) == before
            stock = ledger.get("inventory", "sku", SKU)
            assert (stock["qty"], stock["reason"]) == (0, "no_photo")
    _record(ledger_path, [_row(None, qty="9")])     # the full run under the rule off shows both together
    photo_ready, reason, state, _ = _state(ledger_path)
    assert (photo_ready, reason, state) == (True, None, "dirty")


def test_a_v10_ledger_gains_photo_ready_from_the_stock_reason(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v10 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        now = now_iso()
        for sku, reason in (("SEN-1", "no_photo"), ("SEN-2", None), ("SEN-3", "delisted")):
            lg.upsert("product", {"sku": sku, "source": "sen", "state": "synced", "created_at": now,
                                  "updated_at": now}, pk=("sku",))
            lg.upsert("inventory", {"sku": sku, "qty": 0, "reason": reason, "updated_at": now}, pk=("sku",))
        lg.upsert("product", {"sku": "SEN-4", "source": "sen", "state": "synced", "created_at": now,
                              "updated_at": now}, pk=("sku",))   # no stock row yet
    raw = sqlite3.connect(p)                                                # simulate a v10 ledger
    raw.execute("ALTER TABLE product DROP COLUMN photo_ready")
    raw.execute("PRAGMA user_version = 10")
    raw.execute("UPDATE ledger_meta SET schema_version = 10")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 11
        ready_by = {r["sku"]: r["photo_ready"] for r in lg.execute("SELECT sku, photo_ready FROM product")}
    assert ready_by == {"SEN-1": 0, "SEN-2": 1, "SEN-3": 1, "SEN-4": 1}
