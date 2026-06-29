"""Round-out of Phase 2: the product/inventory bootstrap, discontinued recording,
and the verify gate.

The bootstrap + discontinued lanes are dormant in dev (no products_export), so they
are exercised synthetically here. The verify gate is checked against an emit-written
products CSV, the same comparison it runs after a real build.
"""

from __future__ import annotations

import csv

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import verify as verify_mod
from stone_pipeline.ledger.bootstrap import seed_products, seed_variations
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import (
    populate_discontinued,
    populate_products,
    populate_variations_full,
)
from stone_pipeline.ledger.render import render_products
from stone_pipeline.stages import emit

EXPORT = SETTINGS.paths.variants_export_csv
FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"


def _seed_ids(ledger: Ledger) -> None:
    now = now_iso()
    for cat, val, mid in [("color", "Black", "C1"), ("finish", "Polished", "F1"),
                          ("quality", "First", "Q1"), ("type", "Marble", "T1"),
                          ("category", "Slabs", "PC1")]:
        ledger.upsert("attribute", {"category": cat, "value": val, "medusa_id": mid,
                                    "state": "synced", "created_at": now, "updated_at": now},
                      pk=("category", "value"))
    ledger.upsert("variation", {"key": "slab_marble_x_0001", "branch": "slab", "type": "Marble",
                                "name": "X", "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": "V1",
                                "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now},
                  pk=("key",))


def _product_row(cfg) -> CanonicalRow:
    return CanonicalRow(
        src_site="polonine", surrogate_key="AAA",
        color_name="Black", color_id="C1", finish_name="Polished", finish_id="F1",
        quality_name="First", quality_id="Q1", type_name="Marble", type_id="T1",
        variation_id="V1", variation_name="X", category_pcat_id="PC1",
        title="X Slab", description="d", handle="x-slab-aaa", slug="x-slab-aaa",
        weight=0.3, length=2.5, width=0.2, height=2.0, origin_country_code="IT",
        company_id="CO1", sales_channel_id="SC1", port_ids=["P1"],
        bundle_size=7, sold_in_bundle=True, raw_slab_count="7",
        thumbnail_key="t.jpg", product_image_keys=["i1.jpg"], oriented_image_keys=[],
    )


def test_seed_products_minimal_and_excluded_from_render(tmp_path):
    export = tmp_path / "products_export.csv"
    with export.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["SKU", "Handle", "Inventory"])
        w.writerow(["POLONINE-OLD1", "old-one", "5"])
        w.writerow(["POLONINE-OLD2", "old-two", "0"])
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        n = seed_products(ledger, export)
        assert n == 2
        # minimal rows exist, carry NO variation_key, and seed inventory with no delta
        row = ledger.get("product", "sku", "POLONINE-OLD1")
        assert row is not None and row["variation_key"] is None
        inv = ledger.get("inventory", "sku", "POLONINE-OLD1")
        assert inv["qty"] == 5 and inv["last_synced_qty"] == 5
        # they never render into a product import CSV (no variety link)
        out = tmp_path / "rendered.csv"
        rendered = render_products(ledger, load_source("polonine"), out)
        assert rendered == 0


def test_populate_discontinued_creates_minimal_and_zero_stock(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        n = populate_discontinued(ledger, [("POLONINE-GONE", "gone-handle")])
        assert n == 1
        prod = ledger.get("product", "sku", "POLONINE-GONE")
        assert prod is not None and prod["variation_key"] is None  # FK row, not a real product
        inv = ledger.get("inventory", "sku", "POLONINE-GONE")
        assert inv["qty"] == 0 and inv["last_synced_qty"] is None  # served as a delist


def test_verify_products_passes_and_detects_mismatch(tmp_path):
    cfg = load_source("polonine")
    to_upload = tmp_path / "to_upload"
    to_upload.mkdir()
    rows = [_product_row(cfg)]
    # the real files are named by source NAME (collect_products), which load_source resolves
    emit.write_import_csv(rows, cfg, to_upload / "3_products_polonine.csv")

    ledger_path = tmp_path / "dev.ledger"
    with Ledger.open(ledger_path, env="development") as ledger:
        _seed_ids(ledger)
        populate_products(ledger, rows, cfg)

    with Ledger.open(ledger_path, env="development") as ledger:
        assert verify_mod.verify_products(ledger, to_upload) == []

    # corrupt the correct CSV -> the gate must catch it
    target = to_upload / "3_products_polonine.csv"
    target.write_bytes(target.read_bytes().replace(b"X Slab", b"Y Slab"))
    with Ledger.open(ledger_path, env="development") as ledger:
        assert verify_mod.verify_products(ledger, to_upload), "verify must detect the mismatch"


@pytest.mark.skipif(not (EXPORT.exists() and FULL.exists()),
                    reason="no variants_export.csv + 1_variants_full.csv to check against")
def test_variants_verify_set_equal_after_bootstrap(tmp_path):
    # the integrated case: export-seeded rows (some junk, in_full=0) merged with the
    # produced 1_variants_full (in_full=1); verify_variants must be set-equal.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        seed_variations(ledger)                       # export: all rows, in_full=0
        n = populate_variations_full(ledger, FULL)    # produced set: in_full=1
        assert n > 20000
        assert verify_mod.verify_variants(ledger, FULL.parent) == []
