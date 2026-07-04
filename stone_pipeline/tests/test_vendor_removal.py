"""Two-stage vendor removal: delist (Take offline) then purge + delete config (Remove permanently).
Covers the ledger mechanics -- source-scoped delist to qty 0, the qty-0 delta being served so Medusa
takes them out of sale, the live-products guard, and source-scoped purge that never touches another
vendor."""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import delist_source, live_products, purge_discontinued, ready


def _prod(ledger, sku, source, qty, last):
    """A synced product of `source` with a synced variation and a stock row (last == qty, i.e. not
    already a pending delta), so a later qty change is a clean, observable delta."""
    now = now_iso()
    ledger.upsert("variation", {"key": f"slab_{sku}", "branch": "slab", "type": "", "name": "x",
                                "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": "V", "in_full": 1,
                                "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))
    ledger.upsert("product", {"sku": sku, "source": source, "variation_key": f"slab_{sku}",
                              "state": "synced", "medusa_id": "P", "created_at": now,
                              "updated_at": now}, pk=("sku",))
    ledger.upsert("inventory", {"sku": sku, "qty": qty, "last_synced_qty": last,
                                "updated_at": now}, pk=("sku",))


def test_delist_takes_a_source_offline_and_scopes(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=5, last=5)
        _prod(ledger, "AAA-2", "aaa", qty=3, last=3)
        _prod(ledger, "BBB-1", "bbb", qty=7, last=7)      # another vendor: must stay live

        out = delist_source(ledger, source_codes=["aaa"])
        assert out["delisted"] == 2
        assert set(out["external_ids"]) == {"AAA-1", "AAA-2"}
        assert ledger.get("inventory", "sku", "AAA-1")["qty"] == 0     # aaa -> qty 0
        assert ledger.get("inventory", "sku", "BBB-1")["qty"] == 7     # bbb untouched

        # the qty-0 is a delta the pull now serves, so Medusa takes exactly aaa's products out of sale
        served = {r["external_id"] for r in ready(ledger, "inventory")}
        assert {"AAA-1", "AAA-2"} <= served and "BBB-1" not in served

        # the removal guard: aaa has no live products left, bbb still does
        assert live_products(ledger, source_codes=["aaa"]) == 0
        assert live_products(ledger, source_codes=["bbb"]) == 1


def test_remove_after_delist_purges_only_that_source(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=5, last=5)
        _prod(ledger, "BBB-1", "bbb", qty=7, last=7)
        delist_source(ledger, source_codes=["aaa"])                   # aaa -> qty 0

        purged = purge_discontinued(ledger, source_codes=["aaa"])
        assert purged["product"] == 1 and purged["external_ids"] == ["AAA-1"]
        assert ledger.get("product", "sku", "AAA-1") is None          # gone from the ledger
        assert ledger.get("product", "sku", "BBB-1") is not None      # other vendor intact


def test_delist_is_idempotent_and_reversible(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=5, last=5)
        assert delist_source(ledger, source_codes=["aaa"])["delisted"] == 1
        assert delist_source(ledger, source_codes=["aaa"])["delisted"] == 0   # already qty 0, nothing to do
        # reversible: a re-scrape restocking to qty > 0 is a delta again (Medusa restocks)
        ledger.execute("UPDATE inventory SET qty = 9 WHERE sku = 'AAA-1'")
        assert live_products(ledger, source_codes=["aaa"]) == 1
        assert "AAA-1" in {r["external_id"] for r in ready(ledger, "inventory")}


def test_delete_source_removes_only_that_config_row(tmp_path):
    from stone_pipeline.config import store
    db = tmp_path / "config.db"
    store.upsert_row({"source": "tmpvendor", "enabled": True}, path=db)
    store.upsert_row({"source": "keepvendor", "enabled": True}, path=db)
    assert store.get_row("tmpvendor", path=db) is not None
    assert store.delete_source("tmpvendor", path=db) is True
    assert store.get_row("tmpvendor", path=db) is None                # removed
    assert store.get_row("keepvendor", path=db) is not None           # sibling untouched
    assert store.delete_source("tmpvendor", path=db) is False         # already gone -> False
