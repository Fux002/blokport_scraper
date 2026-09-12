"""The write-through moves `updated_at` only when the row changed (content hash or state).

`updated_at` is the syncing lease clock: reap_stale_syncing returns a served-but-never-acked row to
'dirty' once it is older than the lease. A re-populate that restamps an UNCHANGED row (same
payload_hash, state carried over) would renew a dead lease on every produce, so the row stayed
in-flight for another window each time instead of re-serving. Unchanged rows keep their timestamp;
a content change, and a retiring variety that reappears, still stamp it.
"""

from __future__ import annotations

import csv

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products, populate_variations_full
from stone_pipeline.ledger.sync import reap_stale_syncing
from stone_pipeline.stages import emit
from stone_pipeline.stages.emit_catalog import _COLS

KEY = "slab_marble_arabescato_1124daeb"
DEAD = "2000-01-01T00:00:00+00:00"   # older than any lease


def _write_full(d, name):
    p = d / "1_variants_full.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(_COLS)
        w.writerow([{"Key": KEY, "Name": name}.get(c, "") for c in _COLS])
    return p


def _row(title="Arabescato Polished Slab"):
    return CanonicalRow(
        src_site="polonine", surrogate_key="AAA", color_name="White", finish_name="Polished",
        quality_name="First", type_name="Marble", variation_key=KEY, variation_name="Arabescato",
        title=title, description="d", handle="h", slug="h", weight=1.0, length=2.0, width=1.0,
        height=2.0, company_id="CO1", sales_channel_id="SC1", thumbnail_key="t.jpg",
    )


def _seed_variation(ledger):
    now = now_iso()
    ledger.upsert("variation", {"key": KEY, "branch": "slab", "type": "Marble", "name": "Arabescato",
                                "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": "V1", "in_full": 1,
                                "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def _stamp(ledger, table, state):
    ledger.execute(f"UPDATE {table} SET state = ?, updated_at = ?", (state, DEAD))


def _updated_at(ledger, table):
    return ledger.execute(f"SELECT updated_at FROM {table}").fetchone()["updated_at"]


def test_unchanged_product_keeps_its_timestamp_so_a_dead_lease_is_reaped(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        populate_products(ledger, [_row()], cfg)
        _stamp(ledger, "product", "syncing")           # served, never acked, lease long expired

        populate_products(ledger, [_row()], cfg)       # a produce with the same content
        assert _updated_at(ledger, "product") == DEAD, "an unchanged re-populate renewed the lease"
        assert reap_stale_syncing(ledger, "product") == 1
        assert ledger.get("product", "sku", emit._sku(_row(), cfg))["state"] == "dirty"


def test_changed_product_moves_its_timestamp(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        populate_products(ledger, [_row()], cfg)
        _stamp(ledger, "product", "synced")
        before = now_iso()
        populate_products(ledger, [_row(title="Arabescato Polished Slab v2")], cfg)
        assert _updated_at(ledger, "product") >= before
        assert ledger.get("product", "sku", emit._sku(_row(), cfg))["state"] == "dirty"


def test_unchanged_variation_keeps_its_timestamp_so_a_dead_lease_is_reaped(tmp_path):
    full = _write_full(tmp_path, "Arabescato")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        populate_variations_full(ledger, full)
        _stamp(ledger, "variation", "syncing")

        populate_variations_full(ledger, full)
        assert _updated_at(ledger, "variation") == DEAD, "an unchanged re-populate renewed the lease"
        assert reap_stale_syncing(ledger, "variation") == 1
        assert ledger.get("variation", "key", KEY)["state"] == "dirty"


def test_changed_and_unretired_variations_move_their_timestamp(tmp_path):
    full = _write_full(tmp_path, "Arabescato")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        populate_variations_full(ledger, full)

        _stamp(ledger, "variation", "synced")
        before = now_iso()
        populate_variations_full(ledger, _write_full(tmp_path, "Arabescato Oro"))   # content change
        assert _updated_at(ledger, "variation") >= before
        assert ledger.get("variation", "key", KEY)["state"] == "dirty"

        _stamp(ledger, "variation", "retiring")                                       # then excluded
        before = now_iso()
        populate_variations_full(ledger, _write_full(tmp_path, "Arabescato Oro"))   # reappears unchanged
        assert _updated_at(ledger, "variation") >= before
        assert ledger.get("variation", "key", KEY)["state"] == "dirty"
