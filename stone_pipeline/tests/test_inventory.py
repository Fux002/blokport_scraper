"""Inventory-only lane: the stock refresh produces ONLY the consolidated inventory delta and
never re-imports products or regenerates images. (classify + the inventory-CSV contract are
covered in test_product_state.)"""

from __future__ import annotations

import csv

from stone_pipeline import catalog
from stone_pipeline.run import run_source
from stone_pipeline.stages import emit


def _write_inv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(emit.INVENTORY_COLUMNS)
        w.writerows(rows)


def test_consolidate_inventory_dedups_by_sku_last_run_wins(tmp_path):
    a = tmp_path / "runA" / "inventory_update.csv"
    b = tmp_path / "runB" / "inventory_update.csv"
    _write_inv(a, [["POL-1", "h1", "Default", "5", ""], ["POL-2", "h2", "Default", "3", ""]])
    _write_inv(b, [["POL-1", "h1", "Default", "9", ""]])   # same SKU, newer stock
    out = tmp_path / "to_upload"
    n = catalog.consolidate_inventory([a, b, tmp_path / "missing.csv"], to_upload=out)
    assert n == 2
    rows = list(csv.DictReader((out / "4_inventory_update.csv").open(encoding="utf-8-sig")))
    by = {r["Variant Sku"]: r["Inventory Quantity"] for r in rows}
    assert by == {"POL-1": "9", "POL-2": "3"}              # later run's value wins for POL-1


def test_consolidate_inventory_writes_header_only_when_empty(tmp_path):
    out = tmp_path / "to_upload"
    n = catalog.consolidate_inventory([], to_upload=out)
    assert n == 0
    text = (out / "4_inventory_update.csv").read_text(encoding="utf-8").splitlines()
    assert text == [",".join(emit.INVENTORY_COLUMNS)]       # predictable empty deliverable


def test_minted_surrogate_is_order_independent():
    # the SKU = source_code + surrogate_key must reproduce across re-scrapes so the inventory lane
    # re-targets the SAME existing product. For blank-key rows the surrogate is minted; it must key
    # on a STABLE basis (url/name), NOT the scrape position, or a reordered re-scrape would drift
    # the SKU and the stock update would miss the existing product.
    from stone_pipeline.core.ids import mint_surrogate
    base = mint_surrogate("marenostone", "https://m/p1", "Foo", 3)
    assert base == mint_surrogate("marenostone", "https://m/p1", "Foo", 99)   # ordinal ignored
    assert base != mint_surrogate("marenostone", "https://m/p2", "Foo", 3)    # distinct url -> distinct
    # only when there is no url AND no name does ordinal disambiguate (last resort)
    assert mint_surrogate("s", "", "", 1) != mint_surrogate("s", "", "", 2)


def _known_products_export(tmp_path):
    # a Medusa product export must exist for an inventory refresh to compute a delta against (F9)
    p = tmp_path / "products_export.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Product Handle", "Variant Sku", "Inventory Quantity"])
        w.writerow(["some-slab", "MNS-EXISTING-1", "7"])
    return p


def test_inventory_only_run_skips_images_products_and_canonical(tmp_path):
    # the core guarantee: an inventory refresh writes NO product import and NO canonical (so it
    # neither re-imports products nor feeds the catalog), yet the run completes.
    manifest = run_source("marenostone", inventory_only=True, outputs_dir=tmp_path, state_dir=tmp_path,
                          known_products_path=_known_products_export(tmp_path))
    run_dir = tmp_path / manifest.run_id
    assert not (run_dir / "4_products_import" / "medusa_import.csv").exists()
    assert not (run_dir / "diagnostics" / "canonical.parquet").exists()
    assert (run_dir / "diagnostics" / "manifest.json").exists()  # but it did run


def test_inventory_only_aborts_loud_when_the_export_is_missing(tmp_path):
    # F9: no Medusa export => no delta can be computed. A silent '0 changed' would mask a failed export
    # fetch and drop real stock moves, so the refresh must fail loud instead.
    import pytest
    missing = tmp_path / "does_not_exist.csv"
    with pytest.raises(SystemExit):
        run_source("marenostone", inventory_only=True, outputs_dir=tmp_path, state_dir=tmp_path,
                   known_products_path=missing)


def test_drop_deleted_variants_keeps_imageless_new_but_drops_to_delete(tmp_path):
    # New model: a variety is prepared in Medusa for ALL categories, so an imageless NEW variant (a fan-out
    # block/tile with no product yet) is KEPT (blank image, textured per-category when a product appears).
    # Only variants flagged in variants_to_delete are dropped from the upload files.
    from stone_pipeline import catalog
    import csv as _csv
    delete_file = tmp_path / "variants_to_delete.csv"
    with delete_file.open("w", newline="") as h:
        w = _csv.writer(h); w.writerow(["Key", "Name"]); w.writerow(["slab_junk_del", "Junk"])
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        with (tmp_path / fname).open("w", newline="") as h:
            w = _csv.writer(h); w.writerow(["Key", "Name"])
            w.writerow(["slab_new_imaged", "Imaged"])        # new, product-backed slab -> kept
            w.writerow(["block_new_blank", "Blank"])         # new fan-out, NO image -> KEPT now (was held)
            w.writerow(["slab_junk_del", "Junk"])            # flagged for delete -> dropped
    dropped = catalog.drop_deleted_variants(to_upload=tmp_path, delete_file=delete_file)
    assert dropped == 1
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        keys = {r["Key"] for r in _csv.DictReader((tmp_path / fname).open(encoding="utf-8-sig"))}
        assert "block_new_blank" in keys                     # imageless fan-out record is KEPT (Medusa-prepared)
        assert "slab_new_imaged" in keys
        assert "slab_junk_del" not in keys                   # to-delete junk still dropped


def test_delete_variant_images_dry_run_counts(tmp_path):
    # dry-run lists the variations/<Key>.png it WOULD delete, removing nothing (no boto3 needed).
    from stone_pipeline import delete_variant_images as d
    counts = d.run(["slab_z_aqua_blue_abc", "slab_z_astoria_def"], apply=False)
    assert counts["would-delete"] == 2 and counts["deleted"] == 0
