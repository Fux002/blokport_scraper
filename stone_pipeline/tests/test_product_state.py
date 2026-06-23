"""Item 4: classify scraped products as new vs existing by SKU against a Medusa
product export, and flag inventory changes."""

from __future__ import annotations

import csv

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import product_state


def _row(surrogate, slab_count="5"):
    return CanonicalRow(src_site="polonine", surrogate_key=surrogate, raw_slab_count=slab_count)


def test_known_loader_is_column_tolerant(tmp_path):
    path = tmp_path / "known.csv"
    path.write_text("Product Handle,Variant Sku,Variant Inventory Quantity\n"
                    "h-1,POL-1,7\n", encoding="utf-8")
    known = product_state.load_known_products(path)
    assert "POL-1" in known.by_sku
    assert known.by_sku["POL-1"]["inventory"] == "7"


def test_new_vs_existing_classification(tmp_path):
    cfg = load_source("polonine")
    path = tmp_path / "known.csv"
    # 620 exists with inventory 5 (unchanged), 621 exists with inventory 9 (changed),
    # 999 is not in the export (new)
    path.write_text("Variant Sku,Variant Inventory Quantity\nPOL-620,5\nPOL-621,9\n", encoding="utf-8")
    known = product_state.load_known_products(path)
    rows = [_row("620", "5"), _row("621", "5"), _row("999", "5")]
    stats = product_state.classify(rows, cfg, known)
    assert stats.new == 1 and stats.existing == 2
    by = {r.surrogate_key: r for r in rows}
    assert by["999"].product_status == "new"
    assert by["620"].product_status == "existing" and by["620"].product_changed is False
    assert by["621"].product_status == "existing" and by["621"].product_changed is True
    assert stats.inventory_changed == 1


def test_inventory_csv_matches_medusa_contract(tmp_path):
    from stone_pipeline.stages import emit

    cfg = load_source("polonine")
    row = CanonicalRow(src_site="polonine", surrogate_key="620", handle="alpine-pol-620",
                       raw_slab_count="10")
    path = emit.write_inventory_csv([row], cfg, tmp_path / "inventory_update.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    header = list(rows[0].keys())
    # the importer reads only Variant Sku + Inventory Quantity, but the full
    # structure must be present
    assert header == ["Variant Sku", "Product Handle", "Variant Title",
                      "Inventory Quantity", "Reserved Quantity"]
    assert rows[0]["Variant Sku"] == "POL-620"
    assert rows[0]["Inventory Quantity"] == "10"


def test_discontinued_is_source_scoped_and_scrape_aware(tmp_path):
    # delete loop: an export sku owned by THIS source that the scrape no longer carries is
    # discontinued; a scraped one is kept, and another source's sku is never touched.
    cfg = load_source("polonine")  # source_code 'pol'
    path = tmp_path / "known.csv"
    path.write_text("Variant Sku,Product Handle,Variant Inventory Quantity\n"
                    "POL-1,h1,5\nPOL-2,h2,5\nMAR-9,h9,5\n", encoding="utf-8")
    known = product_state.load_known_products(path)
    gone = product_state.discontinued([_row("1")], cfg, known)   # only POL-1 scraped
    assert {s for s, _ in gone} == {"POL-2"}                     # POL-1 kept, MAR-9 other source
    assert dict(gone)["POL-2"] == "h2"                           # handle carried for the report


def test_discontinued_empty_without_baseline(tmp_path):
    cfg = load_source("polonine")
    known = product_state.load_known_products(tmp_path / "absent.csv")
    assert product_state.discontinued([_row("1")], cfg, known) == []   # never fires without export


def test_inventory_csv_writes_discontinued_at_zero(tmp_path):
    from stone_pipeline.stages import emit
    cfg = load_source("polonine")
    row = CanonicalRow(src_site="polonine", surrogate_key="620", handle="h", raw_slab_count="10")
    p = emit.write_inventory_csv([row], cfg, tmp_path / "inv.csv", discontinued=(("POL-99", "gone-h"),))
    by = {r["Variant Sku"]: r["Inventory Quantity"]
          for r in csv.DictReader(open(p, encoding="utf-8-sig"))}
    assert by["POL-620"] == "10" and by["POL-99"] == "0"   # active keeps real stock; discontinued -> 0


def test_no_export_means_all_new(tmp_path):
    cfg = load_source("polonine")
    known = product_state.load_known_products(tmp_path / "absent.csv")
    rows = [_row("1"), _row("2")]
    stats = product_state.classify(rows, cfg, known)
    assert stats.new == 2 and stats.existing == 0
    assert all(r.product_status == "new" for r in rows)
