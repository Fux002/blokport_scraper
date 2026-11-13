"""A failed apply waits before it is served again (5, 10, 20, 40 min), so a passing hiccup on Medusa's side (an S3
blip while it copies a photo) is retried later instead of failing every immediate re-serve and reaching the
dead-letter cap within seconds. A success, a requeue, a reset or new content ends the wait."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products
from stone_pipeline.tests._ledger_icons import add_icon
from stone_pipeline.ledger.sync import ack, count_ready, ready, record_tombstones, requeue_dead_lettered, reset_sync_state
from stone_pipeline.stages import emit
from stone_pipeline.tests.test_ledger_products import _fixture_rows, _seed_ids

PAST = "2000-01-01T00:00:00+00:00"


def _variation(ledger, key, state, medusa_id=None):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "type": "Marble", "name": "x", "aliases": "[]",
                                "medusa_id": medusa_id, "in_full": 1, "payload_hash": "",
                                "state": state, "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))
    add_icon(ledger, key)


def _product(ledger, sku, variation_key, state):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "s", "variation_key": variation_key, "category": "slabs",
                              "state": state, "created_at": now, "updated_at": now}, pk=("sku",))


def _live_product(ledger):
    _variation(ledger, "marble__v1", state="synced", medusa_id="V1")
    _product(ledger, "P-1", "marble__v1", state="pending")


def _wait_has_passed(ledger, table, pk, value):
    ledger.execute(f"UPDATE {table} SET retry_after = ? WHERE {pk} = ?", (PAST, value))


def test_a_failed_product_is_not_served_again_until_its_wait_has_passed(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _live_product(ledger)
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]
        ack(ledger, "products", "P-1", status_="failed", reason="photo copy timed out")
        assert ledger.get("product", "sku", "P-1")["state"] == "dirty"
        assert ready(ledger, "products") == [] and count_ready(ledger, "products") == 0   # the peek agrees
        _wait_has_passed(ledger, "product", "sku", "P-1")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]


def test_a_failed_variation_waits_too(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__v1", state="pending")
        assert [r["external_id"] for r in ready(ledger, "variations")] == ["marble__v1"]
        ack(ledger, "variations", "marble__v1", status_="failed", reason="Medusa 500")
        assert ready(ledger, "variations") == [] and count_ready(ledger, "variations") == 0
        _wait_has_passed(ledger, "variation", "key", "marble__v1")
        assert [r["external_id"] for r in ready(ledger, "variations")] == ["marble__v1"]


def test_the_wait_doubles_with_each_consecutive_failure_and_the_cap_still_dead_letters(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _live_product(ledger)
        waits = []
        for _ in range(SETTINGS.sync.max_attempts):
            before = datetime.now(timezone.utc)
            ack(ledger, "products", "P-1", status_="failed", reason="photo copy timed out")
            waits.append((datetime.fromisoformat(ledger.get("product", "sku", "P-1")["retry_after"]) - before).total_seconds())
        assert [round(w / 60) for w in waits[:4]] == [5, 10, 20, 40]
        assert ledger.get("product", "sku", "P-1")["state"] == "gap_held"


def test_a_success_ends_the_wait(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _live_product(ledger)
        ack(ledger, "products", "P-1", status_="failed", reason="photo copy timed out")
        ack(ledger, "products", "P-1", medusa_id="M1", status_="synced")
        row = ledger.get("product", "sku", "P-1")
        assert row["state"] == "synced" and row["retry_after"] is None and row["sync_attempts"] == 0


def test_a_requeue_serves_a_dead_letter_at_once(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _live_product(ledger)
        for _ in range(SETTINGS.sync.max_attempts):
            ack(ledger, "products", "P-1", status_="failed", reason="Medusa 400")
        assert requeue_dead_lettered(ledger) == 1
        assert ledger.get("product", "sku", "P-1")["retry_after"] is None
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]


def test_a_reset_serves_everything_from_zero_without_a_wait(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _live_product(ledger)
        ack(ledger, "products", "P-1", status_="failed", reason="photo copy timed out")
        reset_sync_state(ledger)
        assert ledger.get("product", "sku", "P-1")["retry_after"] is None


@pytest.mark.usefixtures("ledger_product_pcats")   # its rows carry PC1 / PC2 (table 577: the registry's)
def test_new_content_is_served_at_once_even_while_a_failure_waits(tmp_path):
    cfg = load_source("polonine")
    rows = _fixture_rows()
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_ids(ledger)
        populate_products(ledger, rows, cfg)
        sku = emit._sku(rows[0], cfg)
        ack(ledger, "products", sku, status_="failed", reason="photo copy timed out")
        populate_products(ledger, rows, cfg)                                   # unchanged: still waits
        assert ledger.get("product", "sku", sku)["retry_after"] is not None
        changed = [rows[0].model_copy(update={"product_image_keys": ["new.jpg"]})] + rows[1:]
        populate_products(ledger, changed, cfg)                                # new photos: no wait
        row = ledger.get("product", "sku", sku)
        assert row["state"] == "dirty" and row["retry_after"] is None


def test_a_blocked_tombstone_waits_and_a_new_one_does_not(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        record_tombstones(ledger, [("P-9", "s")])
        assert [r["external_id"] for r in ready(ledger, "removed")] == ["P-9"]
        ack(ledger, "removed", "P-9", status_="blocked", reason="open reservation")
        assert ready(ledger, "removed") == []
        _wait_has_passed(ledger, "removed", "external_id", "P-9")
        assert [r["external_id"] for r in ready(ledger, "removed")] == ["P-9"]
        ack(ledger, "removed", "P-9", status_="blocked", reason="open reservation")
        record_tombstones(ledger, [("P-9", "s")])                              # re-raised: served at once
        assert [r["external_id"] for r in ready(ledger, "removed")] == ["P-9"]


def test_the_retry_time_sorts_with_the_ledger_s_own_timestamps():
    # retry_after is compared as text with now_iso(): both must be the same ISO form, UTC
    later = sync._retry_after(1)
    assert later > now_iso() and datetime.fromisoformat(later).utcoffset() == timedelta(0)
