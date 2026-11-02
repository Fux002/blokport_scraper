"""A product is live only with a standardised photo (owner 2026-11-02: whatever has no good image is delisted until it is
fixed): with the rule on, a product the image stage found without one goes out at stock 0 with reason 'delisted'
(Blokport hides it) and comes back on the first produce after its photo is published. A run that cannot tell (the photo
pool unreadable, or the stock-only refresh, which skips the image stage) never changes whether a product is hidden, and a
source where the rule would hide nearly everything fails loud instead."""
from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger import populate
from stone_pipeline.ledger.populate import populate_inventory
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
        assert _stock(ledger, rows[-1]) == (0, "delisted") and _stock(ledger, rows[0]) == (3, None)
        _stocked(ledger, rows[:-1] + [_row("x", True)], cfg)
        assert _stock(ledger, rows[-1]) == (3, None)                                # its photo is published: live again


def test_a_run_that_cannot_tell_leaves_a_hidden_product_hidden_and_a_live_one_live(tmp_path, rule_on):
    cfg = load_source("sentas")
    rows = [_row(str(i), True) for i in range(9)] + [_row("x", False)]
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, rows, cfg)
        _stocked(ledger, [_row(str(i), None) for i in range(9)] + [_row("x", None)], cfg)   # stock-only refresh
        assert _stock(ledger, rows[-1]) == (0, "delisted") and _stock(ledger, rows[0]) == (3, None)


def test_the_rule_off_changes_nothing(tmp_path, monkeypatch):
    _rule(monkeypatch, False)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False)], load_source("sentas"))
        assert _stock(ledger, _row("x", False)) == (3, None)


def test_a_source_the_rule_would_almost_empty_fails_loud(tmp_path, rule_on):
    rows = [_row("1", True)] + [_row(str(i), False) for i in range(2, 21)]           # 19 of 20 would be hidden
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        with pytest.raises(RuntimeError, match="would hide"):
            _stocked(ledger, rows, load_source("sentas"))


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
    rows = [CanonicalRow(src_site="sentas", surrogate_key="3", format_value="Slab", image_keys=[_improved(A)])]
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: None)                  # the pool could not be read
    images.run(rows, cfg=cfg)
    assert rows[0].standard_photo is None and rows[0].thumbnail_key == _improved(A)
