"""The product payload carries the photo fact (owner 2026-11-03): Blokport publishes a new priced product straight from the
catalog sync, before any stock delta can tell it the product has no standardised photo, so the product itself says
`photo_ready`, which Blokport reads at create only (the stock lane owns visibility afterwards). It is the no-photo rule's
one decision, stored on the product row: the same decision sends the stock out with reason 'no_photo'. It is outside the
payload hash, so a change of it never re-serves an existing product, while a product not yet pulled is served with the
current value. A stock-only refresh, which cannot tell whether a product has a photo, changes neither."""
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
    with _open(path) as ledger:                     # a synced, textured variety, so its products are servable
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


def _record(path, rows, *, inventory_only=False, discontinued=(), replaced=None):
    assert writethrough.record_source(rows, list(discontinued), load_source("polonine"),
                                      inventory_only=inventory_only, replaced=replaced, path=path)


def _acked(path):
    """Blokport applied the product and its stock."""
    with _open(path) as ledger:
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'prod_a' WHERE sku = ?", (SKU,))
        ledger.execute("UPDATE inventory SET last_synced_qty = qty WHERE sku = ?", (SKU,))


def _state(path):
    """(served photo_ready, or None when the product is not served; stored photo_ready; stock reason; product state;
    payload_hash). The serve is a peek: its lease is rolled back."""
    with _open(path) as ledger:
        product = dict(ledger.get("product", "sku", SKU))
        reason = ledger.get("inventory", "sku", SKU)["reason"]
        served = {r["external_id"]: r for r in ready(ledger, "products")}
        ledger.conn.rollback()
    item = served.get(SKU)
    if item:
        assert item["payload_hash"] == product["payload_hash"]   # the ack's lost-update guard compares these
    return ((item["payload"]["photo_ready"] if item else None), bool(product["photo_ready"]), reason,
            product["state"], product["payload_hash"])


def test_without_a_standardised_photo_the_product_is_not_photo_ready_and_its_stock_is_no_photo(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    served, _, reason, _, _ = _state(ledger_path)
    assert (served, reason) == (False, "no_photo")


def test_with_a_standardised_photo_the_product_is_photo_ready(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(True)])
    served, _, reason, _, _ = _state(ledger_path)
    assert (served, reason) == (True, None)


def test_with_the_rule_off_every_product_is_photo_ready(ledger_path, monkeypatch):
    _rule(monkeypatch, False)
    _record(ledger_path, [_row(False)])
    served, _, reason, _, _ = _state(ledger_path)
    assert (served, reason) == (True, None)


def test_a_new_product_in_a_run_that_cannot_tell_starts_hidden(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(None)])              # the photo pool unread on the run that first produces it
    served, _, reason, _, _ = _state(ledger_path)
    assert (served, reason) == (False, "no_photo")  # created as a hidden draft, the safe side


def test_a_photo_change_on_an_existing_product_moves_the_stock_and_never_re_serves_the_product(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    _, _, _, _, payload_hash = _state(ledger_path)
    for photo, reason in ((True, None), (False, "no_photo")):   # its photo published, then taken off again
        _record(ledger_path, [_row(photo)])
        assert _state(ledger_path) == (None, photo, reason, "synced", payload_hash)
        with _open(ledger_path) as ledger:
            assert [r["external_id"] for r in ready(ledger, "inventory")] == [SKU]   # the stock lane says it
        _acked(ledger_path)


def test_a_product_not_yet_pulled_is_served_with_the_current_value(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _record(ledger_path, [_row(True)])              # its photo published before Blokport's first pull
    served, stored, reason, state, _ = _state(ledger_path)
    assert (served, stored, reason, state) == (True, True, None, "pending")


def test_a_run_that_cannot_tell_keeps_the_photo_fact_of_an_existing_product(ledger_path, monkeypatch):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    _record(ledger_path, [_row(None)])              # the photo pool unread this run
    assert _state(ledger_path)[1:4] == (False, "no_photo", "synced")


@pytest.mark.parametrize("drop", ["discontinued", "replaced_by_lot"])
def test_a_hidden_product_dropped_and_back_in_a_run_that_cannot_tell_stays_hidden(ledger_path, monkeypatch, drop):
    _rule(monkeypatch, True)
    _record(ledger_path, [_row(False)])
    _acked(ledger_path)
    _record(ledger_path, [], discontinued=[(SKU, "h-aaa")],
            replaced={SKU: "L1"} if drop == "replaced_by_lot" else None)
    assert _state(ledger_path)[1:3] == (False, None if drop == "discontinued" else "replaced_by_lot")
    _record(ledger_path, [_row(None)])              # back, in a run whose photo pool was unread
    assert _state(ledger_path)[1:3] == (False, "no_photo")


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
    _record(ledger_path, [_row(None, qty="9")])     # the next full run, the rule still off: shown
    assert _state(ledger_path)[1:4] == (True, None, "synced")


def _v10_ledger(p):
    """A ledger as v10 left it: no photo_ready column, the rule's decision kept only as the stock reason."""
    with Ledger.open(p, env="development") as lg:
        now = now_iso()
        for sku, reason in (("SEN-1", "no_photo"), ("SEN-2", None), ("SEN-3", "replaced_by_lot")):
            lg.upsert("product", {"sku": sku, "source": "sen", "state": "synced", "created_at": now,
                                  "updated_at": now}, pk=("sku",))
            lg.upsert("inventory", {"sku": sku, "qty": 0, "reason": reason, "updated_at": now}, pk=("sku",))
        lg.upsert("product", {"sku": "SEN-4", "source": "sen", "state": "synced", "created_at": now,
                              "updated_at": now}, pk=("sku",))   # no stock row yet
    raw = sqlite3.connect(p)
    raw.execute("ALTER TABLE product DROP COLUMN photo_ready")
    raw.execute("PRAGMA user_version = 10")
    raw.execute("UPDATE ledger_meta SET schema_version = 10")
    raw.commit()
    raw.close()


def _photo_ready_by_sku(lg):
    return {r["sku"]: r["photo_ready"] for r in lg.execute("SELECT sku, photo_ready FROM product")}


needs_drop_column = pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35, 0),
                                       reason="ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v10 shape")


@needs_drop_column
def test_a_v10_ledger_gains_photo_ready_from_the_stock_reason(tmp_path):
    p = tmp_path / "mig.ledger"
    _v10_ledger(p)
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 11
        assert _photo_ready_by_sku(lg) == {"SEN-1": 0, "SEN-2": 1, "SEN-3": 1, "SEN-4": 1}


@needs_drop_column
def test_a_migration_cut_short_after_adding_the_column_fills_it_on_the_next_open(tmp_path, monkeypatch):
    p = tmp_path / "mig.ledger"
    _v10_ledger(p)
    migrate = Ledger._migrate

    def crash_after_migrate(self, *args):
        migrate(self, *args)
        self.conn.close()                           # the process dies before the version bump and the commit
        raise RuntimeError("crashed mid-migration")

    monkeypatch.setattr(Ledger, "_migrate", crash_after_migrate)
    with pytest.raises(RuntimeError):
        Ledger.open(p, env="development")
    monkeypatch.setattr(Ledger, "_migrate", migrate)
    raw = sqlite3.connect(p)                        # the ALTER committed on its own; the version did not move
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 10
    assert "photo_ready" in {r[1] for r in raw.execute("PRAGMA table_info(product)")}
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert _photo_ready_by_sku(lg) == {"SEN-1": 0, "SEN-2": 1, "SEN-3": 1, "SEN-4": 1}
