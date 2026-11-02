"""A product is live only with a standardised photo (owner 2026-11-02: whatever has no good image is delisted until it is
fixed): with the rule on, a product the image stage found without one goes out at stock 0 with reason 'no_photo'
(Blokport hides it; always stock 0, since Blokport reads an unknown reason above 0 as a plain quantity) and comes back on the first produce after its photo is published. A run that cannot tell (the photo
pool unreadable, or the stock-only refresh, which skips the image stage) never changes whether a product is hidden, and a
pool that is empty or unreadable (local mode, nothing published, a failed read) means "cannot tell". Hiding and showing
again are served to Blokport even when the stock stays 0; the dead-stock purge never deletes a hidden product, and a
bundle never redirects to a lot whose pieces are all hidden."""
from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger import populate
from stone_pipeline.ledger.populate import populate_discontinued, populate_inventory
from stone_pipeline.ledger.sync import purge_discontinued, ready
from stone_pipeline.stages import emit, images

A = "a" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


def _row(key, standard_photo, qty="3"):
    return CanonicalRow(src_site="sentas", surrogate_key=key, inventory_quantity=qty, standard_photo=standard_photo)


def _rule(monkeypatch, on):
    monkeypatch.setattr(populate, "SETTINGS", dataclasses.replace(
        SETTINGS, images=dataclasses.replace(SETTINGS.images, require_standard_photo=on)))


@pytest.fixture
def rule_on(monkeypatch):
    _rule(monkeypatch, True)


def _stocked(ledger, rows, cfg):
    """populate_inventory on rows whose products exist (the write-through populates products first)."""
    now = now_iso()
    for r in rows:
        ledger.upsert("product", {"sku": emit._sku(r, cfg), "source": "sen", "state": "synced",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
    return populate_inventory(ledger, rows, cfg)


def _stock(ledger, row):
    r = ledger.get("inventory", "sku", emit._sku(row, load_source("sentas")))
    return r["qty"], r["reason"]


def test_without_a_standardised_photo_a_product_is_delisted_and_comes_back_when_it_has_one(tmp_path, rule_on):
    cfg = load_source("sentas")
    rows = [_row(str(i), True) for i in range(9)] + [_row("x", False)]
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, rows, cfg)
        assert _stock(ledger, rows[-1]) == (0, "no_photo") and _stock(ledger, rows[0]) == (3, None)
        _stocked(ledger, rows[:-1] + [_row("x", True)], cfg)
        assert _stock(ledger, rows[-1]) == (3, None)                                # its photo is published: live again


def test_a_run_that_cannot_tell_leaves_a_hidden_product_hidden_and_a_live_one_live(tmp_path, rule_on):
    cfg = load_source("sentas")
    rows = [_row(str(i), True) for i in range(9)] + [_row("x", False)]
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, rows, cfg)
        _stocked(ledger, [_row(str(i), None) for i in range(9)] + [_row("x", None)], cfg)   # stock-only refresh
        assert _stock(ledger, rows[-1]) == (0, "no_photo") and _stock(ledger, rows[0]) == (3, None)


def test_the_rule_off_changes_nothing(tmp_path, monkeypatch):
    _rule(monkeypatch, False)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False)], load_source("sentas"))
        assert _stock(ledger, _row("x", False)) == (3, None)


def test_hiding_a_product_already_at_stock_0_is_still_served_to_blokport(tmp_path, rule_on):
    cfg = load_source("sentas")
    sold = _row("x", True, qty="0")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [sold], cfg)
        ledger.execute("UPDATE inventory SET last_synced_qty = 0")                    # its stock 0 was acked
        assert ready(ledger, "inventory") == []
        _stocked(ledger, [_row("x", False, qty="0")], cfg)
        assert [r["payload"].get("reason") for r in ready(ledger, "inventory")] == ["no_photo"]


def test_a_hidden_product_its_supplier_drops_becomes_plain_dead_stock(tmp_path, rule_on):
    cfg = load_source("sentas")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False)], cfg)
        sku = emit._sku(_row("x", False), cfg)
        populate_discontinued(ledger, [(sku, "h")])
        assert ledger.get("inventory", "sku", sku)["reason"] is None and purge_discontinued(ledger)["external_ids"] == [sku]


def test_the_dead_stock_purge_keeps_a_hidden_product_and_the_vendor_removal_still_removes_it(tmp_path, rule_on):
    cfg = load_source("sentas")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("hidden", False), _row("live", True)], cfg)
        populate_discontinued(ledger, [("SEN-GONE", "h")])
        assert purge_discontinued(ledger)["external_ids"] == ["SEN-GONE"]           # dead stock only
        assert purge_discontinued(ledger, reason="vendor_removed")["external_ids"] == [emit._sku(_row("hidden", False), cfg)]


def test_a_photo_hidden_product_always_goes_out_at_stock_0(tmp_path, rule_on):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False, qty="7")], load_source("sentas"))
        rows = ledger.execute("SELECT qty, reason FROM inventory WHERE reason = 'no_photo'").fetchall()
        assert [tuple(r) for r in rows] == [(0, "no_photo")]


def test_a_bundle_never_redirects_to_a_lot_with_no_piece_on_sale(tmp_path, rule_on):
    cfg = load_source("sentas")
    piece = _row("2555-4", False)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        now = now_iso()
        ledger.upsert("product", {"sku": "SEN-2555", "source": "sen", "state": "synced", "medusa_id": "m-b",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
        ledger.upsert("inventory", {"sku": "SEN-2555", "qty": 9, "last_synced_qty": 9, "updated_at": now}, pk=("sku",))
        _stocked(ledger, [piece], cfg)
        ledger.execute("UPDATE product SET lot_code = 'L1', medusa_id = 'm-p' WHERE sku = ?", (emit._sku(piece, cfg),))
        now = now_iso()
        ledger.upsert("product", {"sku": "SEN-2555-1", "source": "sen", "state": "synced", "lot_code": "L1",
                                  "medusa_id": "m-sold", "created_at": now, "updated_at": now}, pk=("sku",))
        ledger.upsert("inventory", {"sku": "SEN-2555-1", "qty": 0, "last_synced_qty": 0, "updated_at": now}, pk=("sku",))
        populate_discontinued(ledger, [("SEN-2555", "h")], replaced={"SEN-2555": "L1"})
        assert "SEN-2555" not in [r["external_id"] for r in ready(ledger, "inventory")]   # one hidden, one sold
        _stocked(ledger, [_row("2555-4", True)], cfg)                                   # its photo is published
        assert "SEN-2555" in [r["external_id"] for r in ready(ledger, "inventory")]


def _improved(sha):
    return f"{BASE}/improved/sentas/{sha}.png"


def test_the_image_stage_says_whether_each_product_has_a_standardised_photo_or_that_it_cannot_tell(monkeypatch):
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    rows = [CanonicalRow(src_site="sentas", surrogate_key=k, format_value="Slab", image_keys=[_improved(s)])
            for k, s in (("1", A), ("2", "b" * 64))]
    cfg = type("Cfg", (), {"mode": "passthrough"})()
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: {"main": {A: "e"}, "white": {}, "studio": {}, "close": {}})
    images.run(rows, cfg=cfg)
    assert [r.standard_photo for r in rows] == [True, False]
    for pool in (None, {}, {"main": {}, "white": {}}):                    # unreadable; local mode; nothing published
        rows = [CanonicalRow(src_site="sentas", surrogate_key="3", format_value="Slab", image_keys=[_improved(A)])]
        monkeypatch.setattr(images, "_load_standard_views", lambda cfg, pool=pool: pool)
        images.run(rows, cfg=cfg)
        assert rows[0].standard_photo is None and rows[0].thumbnail_key == _improved(A)
