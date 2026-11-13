"""A listing gone at its supplier stays published as sold out (owner's ruling 11-11; table 505, agreed with M4).

- A published product the scrape dropped goes out at stock 0 with reason 'sold_out' and `gone_at`, the first trusted
  scrape it was missing from, kept while it stays gone; a hidden one keeps its reason (no_photo, delisted) and gets
  `gone_at` too; a bundle replaced by its lot stays 'replaced_by_lot'.
- It reappears: normal stock, no reason, no `gone_at`, the same SKU and product row.
- Nothing deletes a gone listing's record: the dead-stock purge deletes only bundles replaced by their lot, a vendor
  removal unpublishes (reason 'delisted', no tombstone), and a hard or factory reset and a seed drop spare it.
- The inventory delta carries `gone_at`; the ledger gains the column in place (v19, after P2-10 C4's v18).
Fixtures only."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import populate
from stone_pipeline.ledger.db import HIDDEN_REASONS, SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_discontinued, populate_inventory, populate_products
from stone_pipeline.ledger.sync import (
    purge_discontinued, ready, reconcile_variations_to_seed, reset_sync_state, unpublish_source,
)
from stone_pipeline.stages import emit, product_state
from stone_pipeline.stages.product_state import KnownProducts

CFG = SourceConfig(source="sentas", source_code="sen")
FIRST = "2026-11-01T00:00:00Z"


def _variation(ledger, key="marble__v_1"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "type": "", "name": "x", "aliases": "[]", "medusa_id": "V",
                                "in_full": 1, "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def _prod(ledger, sku, qty=5, reason=None, source="sen", gone_at=None, key="marble__v_1"):
    """A synced product Medusa holds, its stock synced as it is (so a change is a clean delta)."""
    now = now_iso()
    _variation(ledger, key)
    ledger.upsert("product", {"sku": sku, "source": source, "variation_key": key, "state": "synced",
                              "medusa_id": "P-" + sku, "last_synced": now, "created_at": now, "updated_at": now},
                  pk=("sku",))
    ledger.upsert("inventory", {"sku": sku, "qty": qty, "made_to_order": 0, "reason": reason, "gone_at": gone_at,
                                "last_synced_qty": qty, "last_synced_made_to_order": 0, "last_synced_reason": reason,
                                "updated_at": now}, pk=("sku",))


def _stock(ledger, sku):
    r = ledger.get("inventory", "sku", sku)
    return (r["qty"], r["reason"], r["gone_at"]) if r else None


def _open(tmp_path):
    return Ledger.open(tmp_path / "l.db", env="development")


# --- marking it gone ------------------------------------------------------------------------------------------------
def test_a_published_product_the_scrape_dropped_is_sold_out_with_its_gone_at(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1")
        populate_discontinued(ledger, [("SEN-1", "h")])
        qty, reason, gone_at = _stock(ledger, "SEN-1")
        assert (qty, reason) == (0, "sold_out") and gone_at
        assert ledger.get("product", "sku", "SEN-1")["medusa_id"] == "P-SEN-1"      # the record stays


def test_a_listing_still_gone_keeps_its_first_gone_at(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1", qty=0, reason="sold_out", gone_at=FIRST)
        populate_discontinued(ledger, [("SEN-1", "h")])
        assert _stock(ledger, "SEN-1") == (0, "sold_out", FIRST)


@pytest.mark.parametrize("hidden", ["no_photo", "delisted", "held"])
def test_a_hidden_listing_gone_keeps_its_publish_state_and_gets_gone_at(tmp_path, hidden):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1", qty=0, reason=hidden)
        populate_discontinued(ledger, [("SEN-1", "h")])
        qty, reason, gone_at = _stock(ledger, "SEN-1")
        assert (qty, reason) == (0, hidden) and gone_at


def test_the_hidden_reasons_are_the_hide_set_agreed_with_m4():
    assert set(HIDDEN_REASONS) == {"no_photo", "delisted", "held"}   # 'held' is #632's (stock unknown)


def test_a_bundle_replaced_by_its_lot_stays_replaced_and_is_not_gone(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1")
        populate_discontinued(ledger, [("SEN-1", "h")], replaced={"SEN-1": "LOT-1"})
        assert _stock(ledger, "SEN-1") == (0, "replaced_by_lot", None)


def test_a_dropped_product_the_ledger_never_held_is_sold_out(tmp_path):
    with _open(tmp_path) as ledger:
        populate_discontinued(ledger, [("SEN-9", "h")])
        qty, reason, gone_at = _stock(ledger, "SEN-9")
        assert (qty, reason) == (0, "sold_out") and gone_at


def test_a_listing_that_reappears_returns_to_normal_stock_under_the_same_record(tmp_path, monkeypatch):
    monkeypatch.setattr(populate, "SETTINGS", dataclasses.replace(
        SETTINGS, images=dataclasses.replace(SETTINGS.images, require_standard_photo=False)))
    row = CanonicalRow(src_site="sentas", surrogate_key="x", inventory_quantity="4")
    sku = emit._sku(row, CFG)
    with _open(tmp_path) as ledger:
        populate_products(ledger, [row], CFG)
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'M-1', last_synced = ?", (now_iso(),))
        populate_discontinued(ledger, [(sku, "h")])
        assert _stock(ledger, sku)[1] == "sold_out"
        populate_inventory(ledger, [row], CFG)
        assert _stock(ledger, sku) == (4, None, None)
        assert ledger.get("product", "sku", sku)["medusa_id"] == "M-1"
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0


# --- nothing deletes it -----------------------------------------------------------------------------------------------
def test_the_purge_deletes_only_bundles_replaced_by_their_lot(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-SOLD", qty=0, reason="sold_out", gone_at=FIRST)
        _prod(ledger, "SEN-HID", qty=0, reason="no_photo", gone_at=FIRST)
        _prod(ledger, "SEN-DEL", qty=0, reason="delisted")
        _prod(ledger, "SEN-OLD", qty=0)                                   # gone before this rule: no reason yet
        _prod(ledger, "SEN-LOT", qty=0, reason="replaced_by_lot")
        out = purge_discontinued(ledger)
        assert out["external_ids"] == ["SEN-LOT"]
        assert [r["external_id"] for r in ledger.execute("SELECT external_id FROM removed")] == ["SEN-LOT"]
        for sku in ("SEN-SOLD", "SEN-HID", "SEN-DEL", "SEN-OLD"):
            assert ledger.get("product", "sku", sku) is not None


def test_a_vendor_removal_unpublishes_every_listing_and_deletes_nothing(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1", qty=0, reason="delisted")                  # taken offline first
        _prod(ledger, "SEN-2", qty=0, reason="sold_out", gone_at=FIRST)
        _prod(ledger, "SEN-3", qty=0, reason="no_photo")
        _prod(ledger, "BBB-1", qty=7, source="bbb")
        out = unpublish_source(ledger, source_codes=["sen"])
        assert sorted(out["external_ids"]) == ["SEN-2", "SEN-3"]          # SEN-1 is delisted already
        assert _stock(ledger, "SEN-2") == (0, "delisted", FIRST)
        assert _stock(ledger, "SEN-3")[1] == "delisted"
        assert _stock(ledger, "BBB-1") == (7, None, None)
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0
        assert ledger.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 4
        served = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert served["SEN-2"]["reason"] == "delisted" and "BBB-1" not in served
        assert unpublish_source(ledger, source_codes=["sen"])["external_ids"] == []      # idempotent


def test_a_vendor_removal_through_the_lifecycle_unpublishes_and_drops_the_config(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    store.upsert_row({"source": "zztest", "source_code": "zzt", "vendor": "Junk"})
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "ZZT-1", qty=0, reason="sold_out", gone_at=FIRST, source="zzt")
    body, code = lifecycle.remove_source("zztest")
    assert code == 200 and body["config_removed"] is True and body["unpublished"] == 1
    assert body["external_ids"] == ["ZZT-1"] and "purged" not in body
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _stock(ledger, "ZZT-1") == (0, "delisted", FIRST)
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0


@pytest.mark.parametrize("scoped", [False, True])
def test_a_hard_reset_spares_gone_listings_and_tombstones_the_rest(tmp_path, scoped):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-GONE", qty=0, reason="sold_out", gone_at=FIRST)
        _prod(ledger, "SEN-HID", qty=0, reason="no_photo", gone_at=FIRST)
        _prod(ledger, "SEN-LIVE", qty=5)
        out = reset_sync_state(ledger, source_codes=["sen"] if scoped else None, hard=True)
        assert [r["external_id"] for r in ledger.execute("SELECT external_id FROM removed")] == ["SEN-LIVE"]
        assert out["product_tombstoned"] == 1 and out["records_kept"] == 2
        assert _stock(ledger, "SEN-GONE") == (0, "sold_out", FIRST) and ledger.get("product", "sku", "SEN-GONE")
        assert _stock(ledger, "SEN-HID") == (0, "no_photo", FIRST)
        assert ledger.get("product", "sku", "SEN-LIVE") is None


def test_a_gone_listing_a_hard_reset_spares_is_served_again_once_from_zero(tmp_path):
    # a factory reset is coordinated: Medusa is wiped too, so the spared record must re-serve (review M2 on #634)
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-GONE", qty=0, reason="sold_out", gone_at=FIRST)
        reset_sync_state(ledger, hard=True)
        product, stock = ledger.get("product", "sku", "SEN-GONE"), ledger.get("inventory", "sku", "SEN-GONE")
        assert (product["state"], product["medusa_id"], product["last_synced"]) == ("pending", None, None)
        assert (stock["last_synced_qty"], stock["last_synced_reason"]) == (None, None)
        assert _stock(ledger, "SEN-GONE") == (0, "sold_out", FIRST)
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'P2', last_synced = ? WHERE sku = 'SEN-GONE'",
                       (now_iso(),))                                    # Blokport creates it again and acks
        served = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert served["SEN-GONE"] == {"sku": "SEN-GONE", "quantity": 0, "made_to_order": False, "reason": "sold_out",
                                      "gone_at": FIRST}


@pytest.mark.parametrize("scoped", [False, True])
def test_a_hard_reset_spares_a_vendor_removed_listing_and_re_serves_it_once(tmp_path, scoped):
    # Mas: a vendor-removed (delisted) listing's record survives a factory reset, as a gone one's does
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-DEL", qty=0, reason="delisted")
        _prod(ledger, "SEN-LIVE", qty=5)
        out = reset_sync_state(ledger, source_codes=["sen"] if scoped else None, hard=True)
        assert [r["external_id"] for r in ledger.execute("SELECT external_id FROM removed")] == ["SEN-LIVE"]
        assert out["records_kept"] == 1 and _stock(ledger, "SEN-DEL") == (0, "delisted", None)
        product, stock = ledger.get("product", "sku", "SEN-DEL"), ledger.get("inventory", "sku", "SEN-DEL")
        assert (product["state"], product["medusa_id"]) == ("pending", None) and stock["last_synced_reason"] is None


def test_a_seed_drop_keeps_a_variety_a_vendor_removed_listing_shows(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-DEL", qty=0, reason="delisted", key="marble__del_1")
        out = reconcile_variations_to_seed(ledger, seed_keys=set(), protected=set(), seed_identities=set())
        assert out["varieties_kept"] == ["marble__del_1"] and ledger.get("product", "sku", "SEN-DEL") is not None


def test_a_seed_drop_keeps_a_variety_a_gone_listing_shows(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-GONE", qty=0, reason="sold_out", gone_at=FIRST, key="marble__gone_1")
        _prod(ledger, "SEN-LIVE", qty=5, key="marble__live_2")
        out = reconcile_variations_to_seed(ledger, seed_keys=set(), protected=set(), seed_identities=set())
        assert out["varieties_kept"] == ["marble__gone_1"] and "marble__gone_1" not in out["dropped_keys"]
        assert ledger.get("variation", "key", "marble__gone_1") is not None
        assert ledger.get("product", "sku", "SEN-GONE") is not None
        assert ledger.get("variation", "key", "marble__live_2") is None


@pytest.mark.parametrize("force", [False, True])
def test_a_variety_a_gone_listing_shows_cannot_be_retired_and_nothing_is_deleted(tmp_path, force):
    # manager's ruling on #634: one rule, fails loud, the operator decides first (force does not override it)
    from stone_pipeline.ledger.sync import KeptListingsShowVariety, retire_variation
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-GONE", qty=0, reason="sold_out", gone_at=FIRST)
        with pytest.raises(KeptListingsShowVariety, match="SEN-GONE"):
            retire_variation(ledger, "marble__v_1", force=force)
        assert ledger.get("product", "sku", "SEN-GONE") is not None
        assert ledger.get("variation", "key", "marble__v_1") is not None
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0


@pytest.mark.parametrize("force", [False, True])
def test_a_variety_a_vendor_removed_listing_shows_cannot_be_retired_either(tmp_path, force):
    # review LOW on #634: one rule with the reset (db.KEPT_SKUS_SQL): a delisted record is kept, so its variety stays
    from stone_pipeline.ledger.sync import KeptListingsShowVariety, retire_variation
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-DEL", qty=0, reason="delisted")
        with pytest.raises(KeptListingsShowVariety, match="SEN-DEL"):
            retire_variation(ledger, "marble__v_1", force=force)
        assert ledger.get("product", "sku", "SEN-DEL") is not None
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0


def test_the_retire_verb_answers_409_naming_the_gone_listings(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _prod(ledger, "SEN-GONE", qty=0, reason="sold_out", gone_at=FIRST)
    body, code = lifecycle.retire_variation("marble__v_1")
    assert code == 409 and "SEN-GONE" in body["error"]


def test_a_variety_whose_gone_listing_came_back_retires_as_before(tmp_path):
    from stone_pipeline.ledger.sync import retire_variation
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1", qty=0)                                     # stock 0, not gone
        retire_variation(ledger, "marble__v_1")
        assert ledger.get("product", "sku", "SEN-1") is None


# --- what Blokport receives ------------------------------------------------------------------------------------------
def test_the_delta_carries_sold_out_and_gone_at_and_a_hidden_one_keeps_its_reason(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1")
        _prod(ledger, "SEN-2", qty=0, reason="no_photo")
        ledger.execute("UPDATE inventory SET gone_at = ? WHERE sku = 'SEN-1'", (FIRST,))   # pinned for the payload
        ledger.execute("UPDATE inventory SET qty = 0, reason = 'sold_out' WHERE sku = 'SEN-1'")
        ledger.execute("UPDATE inventory SET gone_at = ?, last_synced_reason = NULL WHERE sku = 'SEN-2'", (FIRST,))
        served = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert served["SEN-1"] == {"sku": "SEN-1", "quantity": 0, "made_to_order": False, "reason": "sold_out",
                                   "gone_at": FIRST}
        assert served["SEN-2"] == {"sku": "SEN-2", "quantity": 0, "made_to_order": False, "reason": "no_photo",
                                   "gone_at": FIRST}


def test_a_reappeared_listing_s_delta_has_no_reason_and_no_gone_at(tmp_path):
    with _open(tmp_path) as ledger:
        _prod(ledger, "SEN-1", qty=0, reason="sold_out", gone_at=FIRST)
        ledger.execute("UPDATE inventory SET qty = 3, reason = NULL, gone_at = NULL WHERE sku = 'SEN-1'")
        served = {i["external_id"]: i["payload"] for i in ready(ledger, "inventory")}
        assert served["SEN-1"] == {"sku": "SEN-1", "quantity": 3, "made_to_order": False}


# --- the delete-loop report ------------------------------------------------------------------------------------------
def test_the_report_lists_only_the_listings_this_run_takes_off_sale():
    known = KnownProducts()
    known.by_sku.update({"SEN-1": {"inventory": "4", "handle": "a"}, "SEN-2": {"inventory": "0", "handle": "b"}})
    gone = [("SEN-1", "a"), ("SEN-2", "b")]
    assert product_state.newly_gone(gone, CFG, known) == [("SEN-1", "a")]


# --- the ledger ------------------------------------------------------------------------------------------------------
def test_a_v17_ledger_gains_gone_at_in_place_and_keeps_its_data(tmp_path):
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _prod(lg, "SEN-1", qty=0)
    raw = sqlite3.connect(p)                                         # simulate a v17 ledger
    raw.execute("ALTER TABLE inventory DROP COLUMN gone_at")
    raw.execute("PRAGMA user_version = 17")
    raw.execute("UPDATE ledger_meta SET schema_version = 17")
    raw.commit()
    raw.close()
    for _ in range(2):                                               # the second open is a no-op
        with Ledger.open(p, env="development") as lg:
            assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 18
            assert lg.get("ledger_meta", "id", 1)["schema_version"] == SCHEMA_VERSION
            assert _stock(lg, "SEN-1") == (0, None, None)


def test_gone_at_is_the_step_after_c4_s_v18(tmp_path):
    # ledger order (scraper manager, 13 Nov): P2-10 C4's v18 (variation.alias_rows + codes) comes first in the window,
    # so the sold-out column is v19; a v18 ledger gains gone_at in place, and a second open is a no-op
    p = tmp_path / "v18.ledger"
    with Ledger.open(p, env="development") as lg:
        _prod(lg, "SEN-1", qty=0)
    raw = sqlite3.connect(p)                                         # simulate a v18 ledger
    raw.execute("ALTER TABLE inventory DROP COLUMN gone_at")
    raw.execute("PRAGMA user_version = 18")
    raw.execute("UPDATE ledger_meta SET schema_version = 18")
    raw.commit()
    raw.close()
    for _ in range(2):
        with Ledger.open(p, env="development") as lg:
            assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 19
            assert "gone_at" in {r["name"] for r in lg.conn.execute("PRAGMA table_info(inventory)")}
            assert _stock(lg, "SEN-1") == (0, None, None)


def test_the_contract_states_sold_out_gone_at_and_the_purge_scope():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    contract = (root / "INTEGRATION_CONTRACT.md").read_text(encoding="utf-8")
    assert "## S-7" in contract and '"sold_out"' in contract and "gone_at" in contract
    assert "never purges a gone listing" in contract
    api = (root / "FRONTEND_API.md").read_text(encoding="utf-8")
    assert "unpublish its products (reason `delisted`) + drop config" in api
