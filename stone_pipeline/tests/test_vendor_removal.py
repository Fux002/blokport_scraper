"""Two-stage vendor removal: delist (Take offline) then unpublish + delete config (Remove permanently; a removal
unpublishes every listing and deletes none, table 505). Covers the ledger mechanics -- source-scoped delist to qty 0,
the qty-0 delta being served so Medusa takes them out of sale, the live-products guard, and the purge, which deletes
only bundles replaced by their lot and never touches another vendor."""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import (
    ack,
    delist_source,
    failures,
    live_products,
    purge_discontinued,
    ready,
)


def _prod(ledger, sku, source, qty, last):
    """A synced product of `source` with a synced variation and a stock row (last == qty, i.e. not
    already a pending delta), so a later qty change is a clean, observable delta."""
    now = now_iso()
    ledger.upsert("variation", {"key": f"marble__{sku}", "type": "", "name": "x",
                                "alias_rows": "[]", "codes": "[]",
                                "medusa_id": "V", "in_full": 1,
                                "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))
    ledger.upsert("product", {"sku": sku, "source": source, "variation_key": f"marble__{sku}",
                              "state": "synced", "medusa_id": "P", "created_at": now,
                              "updated_at": now}, pk=("sku",))
    ledger.upsert("inventory", {"sku": sku, "qty": qty, "last_synced_qty": last,
                                "last_synced_made_to_order": None if last is None else 0,
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


def test_a_purge_after_take_offline_keeps_the_delisted_records(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=5, last=5)
        _prod(ledger, "BBB-1", "bbb", qty=7, last=7)
        delist_source(ledger, source_codes=["aaa"])                   # aaa -> qty 0, reason delisted

        purged = purge_discontinued(ledger, source_codes=["aaa"])
        assert purged["product"] == 0 and purged["external_ids"] == []
        assert ledger.get("product", "sku", "AAA-1") is not None      # hidden, its record kept
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


def test_purge_records_tombstones_served_on_the_removed_lane(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=0, last=0)
        ledger.execute("UPDATE inventory SET reason = 'replaced_by_lot' WHERE sku = 'AAA-1'")   # a replaced bundle
        out = purge_discontinued(ledger, source_codes=["aaa"])
        assert out["external_ids"] == ["AAA-1"]
        # the delete is delivered to Medusa as a pulled tombstone, not a call-response
        served = ready(ledger, "removed")
        assert served == [{"external_id": "AAA-1",
                           "payload": {"kind": "product", "sku": "AAA-1", "source": "aaa",
                                       "reason": "discontinued"}}]


def test_variation_tombstone_lane_orders_before_products_and_deletes_the_row(tmp_path):
    # the ONE removed lane carries product AND variation tombstones (kind), products serve FIRST (E1),
    # and a 'done' ack on a variation tombstone hard-deletes the (childless, retiring) variety row (E3).
    from stone_pipeline.ledger.sync import record_tombstones
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("variation", {"key": "marble__x_1", "type": "Marble",
                                    "name": "X", "alias_rows": "[]", "codes": "[]",
                                    "medusa_id": "VID", "in_full": 0,
                                    "payload_hash": "", "state": "retiring", "first_seen": now,
                                    "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))
        record_tombstones(ledger, [("AAA-1", "aaa")], reason="vendor_removed", kind="product")
        record_tombstones(ledger, [("marble__x_1", None)], reason="variation_removed", kind="variation")
        served = ready(ledger, "removed")
        assert [t["payload"]["kind"] for t in served] == ["product", "variation"]        # E1 order
        assert served[1]["payload"] == {"kind": "variation", "key": "marble__x_1",
                                        "reason": "variation_removed"}
        assert ack(ledger, "removed", "marble__x_1", status_="done") == 1
        assert ledger.get("variation", "key", "marble__x_1") is None                 # variety row gone
        assert [t["external_id"] for t in ready(ledger, "removed")] == ["AAA-1"]          # product tombstone remains


def test_retire_variation_cascades_and_tombstones_a_synced_variety(tmp_path):
    # explicit variety removal: force-cascade its live product (tombstoned), tombstone the variety (E2 synced) +
    # hold it 'retiring' (its icons stay with the row), and on ack-done hard-delete the row and its icons (E3).
    from stone_pipeline.ledger.sync import retire_variation, variation_live_products
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("variation", {"key": "K", "type": "Marble", "name": "X",
                                    "alias_rows": "[]", "codes": "[]",
                                    "medusa_id": "VID", "in_full": 1, "payload_hash": "",
                                    "state": "synced", "first_seen": now, "last_synced": now,
                                    "created_at": now, "updated_at": now}, pk=("key",))
        ledger.upsert("product", {"sku": "ZUC-1", "source": "zuc", "variation_key": "K", "state": "synced",
                                  "medusa_id": "P", "created_at": now, "updated_at": now}, pk=("sku",))
        ledger.upsert("inventory", {"sku": "ZUC-1", "qty": 5, "last_synced_qty": 5, "last_synced_made_to_order": 0,
                                    "updated_at": now}, pk=("sku",))
        ledger.execute("INSERT INTO variation_category_image (key, category_handle, s3_key, etag) "
                       "VALUES ('K', 'slabs', 'dev/variations/K/slab.png', 'e1')")
        assert variation_live_products(ledger, "K") == 1
        out = retire_variation(ledger, "K", force=True)
        assert out["products_removed"] == 1 and out["tombstoned_variation"] is True
        assert ledger.get("variation", "key", "K")["state"] == "retiring"
        assert ledger.get("product", "sku", "ZUC-1") is None
        icons = "SELECT COUNT(*) n FROM variation_category_image WHERE key = 'K'"
        assert ledger.execute(icons).fetchone()["n"] == 1                              # held with the row
        served = ready(ledger, "removed")
        assert [t["payload"]["kind"] for t in served] == ["product", "variation"]      # E1 order
        assert ack(ledger, "removed", "ZUC-1", status_="done") == 1
        assert ack(ledger, "removed", "K", status_="done") == 1
        assert ledger.get("variation", "key", "K") is None                             # row gone (E3)
        assert ledger.execute(icons).fetchone()["n"] == 0                              # its icons with it


def test_ack_done_keeps_tombstone_if_variety_re_acquired_a_child(tmp_path):
    # self-heal (F5): a raced produce could re-link a product onto a 'retiring' Key before the retired-key
    # exclusion caught up. ack-done must NOT blindly DELETE the variety (FK-fail -> orphaned row, lost
    # tombstone); it keeps the pending tombstone until the variety is childless again, then completes.
    from stone_pipeline.ledger.sync import retire_variation
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("variation", {"key": "K", "type": "Marble", "name": "X",
                                    "alias_rows": "[]", "codes": "[]",
                                    "medusa_id": "VID", "in_full": 0, "payload_hash": "",
                                    "state": "synced", "first_seen": now, "last_synced": now,
                                    "created_at": now, "updated_at": now}, pk=("key",))
        retire_variation(ledger, "K")                                          # no products -> retiring + tombstone
        assert ledger.get("variation", "key", "K")["state"] == "retiring"
        ledger.upsert("product", {"sku": "ZUC-9", "source": "zuc", "variation_key": "K", "state": "pending",
                                  "created_at": now, "updated_at": now}, pk=("sku",))   # raced re-link
        assert ack(ledger, "removed", "K", status_="done") == 0                # refused: kept, not deleted
        assert ledger.get("variation", "key", "K") is not None                 # variety NOT orphan-deleted
        assert ledger.get("removed", "external_id", "K")["sync_attempts"] == 1  # tombstone kept, waits like a block
        ledger.execute("UPDATE removed SET retry_after = '2000-01-01T00:00:00+00:00'")
        assert [t["external_id"] for t in ready(ledger, "removed")] == ["K"]   # re-serves after the wait
        ledger.execute("DELETE FROM product WHERE sku = 'ZUC-9'")              # next produce moved the child off
        assert ack(ledger, "removed", "K", status_="done") == 1                # now childless -> completes
        assert ledger.get("variation", "key", "K") is None


def test_retire_never_synced_variety_drops_it_without_a_tombstone(tmp_path):
    # a variety Medusa never received (medusa_id NULL) has nothing to delete -> drop locally, no tombstone (E2)
    from stone_pipeline.ledger.sync import retire_variation
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("variation", {"key": "NS", "type": "Marble", "name": "Y",
                                    "alias_rows": "[]", "codes": "[]",
                                    "medusa_id": None, "in_full": 1, "payload_hash": "",
                                    "state": "pending", "first_seen": now, "last_synced": None,
                                    "created_at": now, "updated_at": now}, pk=("key",))
        out = retire_variation(ledger, "NS")
        assert out["tombstoned_variation"] is False
        assert ledger.get("variation", "key", "NS") is None
        assert ready(ledger, "removed") == []


def test_un_retire_clears_the_tombstone_and_re_serves(tmp_path):
    from stone_pipeline.ledger.sync import retire_variation, un_retire_variation
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("variation", {"key": "K", "type": "Marble", "name": "X",
                                    "alias_rows": "[]", "codes": "[]",
                                    "medusa_id": "VID", "in_full": 1, "payload_hash": "",
                                    "state": "synced", "first_seen": now, "last_synced": now,
                                    "created_at": now, "updated_at": now}, pk=("key",))
        retire_variation(ledger, "K")
        assert ledger.get("variation", "key", "K")["state"] == "retiring"
        assert [t["external_id"] for t in ready(ledger, "removed")] == ["K"]
        out = un_retire_variation(ledger, "K")
        assert out["tombstone_cleared"] == 1
        assert ready(ledger, "removed") == []                                          # E5: tombstone cleared
        assert ledger.get("variation", "key", "K")["state"] == "dirty"                 # re-serves


def test_tombstone_done_retires_blocked_retries_and_dead_letters(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "AAA-1", "aaa", qty=0, last=0)
        _prod(ledger, "AAA-2", "aaa", qty=0, last=0)
        ledger.execute("UPDATE inventory SET reason = 'replaced_by_lot'")      # the purge's dead stock
        purge_discontinued(ledger, source_codes=["aaa"])

        # 'done' retires the tombstone -> no longer served
        assert ack(ledger, "removed", "AAA-1", status_="done") == 1
        assert {t["external_id"] for t in ready(ledger, "removed")} == {"AAA-2"}

        # 'blocked' (open reservation) keeps it and re-serves once its wait has passed, until it dead-letters after
        # the cap
        for _ in range(SETTINGS.sync.max_attempts - 1):
            ack(ledger, "removed", "AAA-2", status_="blocked", reason="reserved")
            assert ready(ledger, "removed") == []                                        # waiting
            ledger.execute("UPDATE removed SET retry_after = '2000-01-01T00:00:00+00:00' WHERE external_id = 'AAA-2'")
            assert {t["external_id"] for t in ready(ledger, "removed")} == {"AAA-2"}   # still retrying
        ack(ledger, "removed", "AAA-2", status_="blocked", reason="reserved")          # hits the cap
        assert ready(ledger, "removed") == []                                          # dead -> stops serving
        assert any(f["type"] == "removed" and f["external_id"] == "AAA-2" for f in failures(ledger))


def test_repopulating_a_sku_clears_its_tombstone(tmp_path):
    # a re-added vendor reuses deterministic SKUs: a live product must never keep a pending tombstone,
    # or Medusa's /removed pull would delete the freshly re-created product.
    from stone_pipeline.ledger.db import now_iso as _now
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        ledger.upsert("removed", {"external_id": "AAA-1", "source": "aaa", "reason": "vendor_removed",
                                  "state": "pending", "sync_attempts": 0, "sync_error": None,
                                  "created_at": _now(), "updated_at": _now()}, pk=("external_id",))
        assert len(ready(ledger, "removed")) == 1
        _prod(ledger, "AAA-1", "aaa", qty=9, last=None)          # re-created (still a bare upsert here)
        # simulate populate's invariant sweep (it runs this after upserting products)
        ledger.execute("DELETE FROM removed WHERE external_id IN (SELECT sku FROM product)")
        assert ready(ledger, "removed") == []                    # tombstone cleared, product stays live


def test_delist_flags_inventory_reason_ordinary_oos_does_not(tmp_path):
    # Medusa hides a qty-0 that carries reason='delisted'. A qty-0 with no reason carries no reason key (Blokport hides
    # it today, measured by M4; table 505). The marker rides the inventory delta, scoped by SKU.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "DEL-1", "aaa", qty=5, last=5)          # will be delisted (take offline)
        _prod(ledger, "OOS-1", "bbb", qty=0, last=5)          # ordinary out-of-stock (no reason)
        delist_source(ledger, source_codes=["aaa"])
        served = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert served["DEL-1"] == {"sku": "DEL-1", "quantity": 0, "made_to_order": False,
                                    "reason": "delisted"}
        assert served["OOS-1"] == {"sku": "OOS-1", "quantity": 0, "made_to_order": False}   # no reason key

        # a re-scrape restocks (qty>0) and clears the marker -> Medusa republishes
        ledger.execute("UPDATE inventory SET qty = 8, reason = NULL WHERE sku = 'DEL-1'")
        back = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert back["DEL-1"] == {"sku": "DEL-1", "quantity": 8, "made_to_order": False}   # back in stock, no reason


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
