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


def test_inventory_only_run_skips_images_products_and_canonical(tmp_path):
    # the core guarantee: an inventory refresh writes NO product import and NO canonical (so it
    # neither re-imports products nor feeds the catalog), yet the run completes.
    manifest = run_source("marenostone", inventory_only=True,
                          outputs_dir=tmp_path, state_dir=tmp_path)
    run_dir = tmp_path / manifest.run_id
    assert not (run_dir / "4_products_import" / "medusa_import.csv").exists()
    assert not (run_dir / "diagnostics" / "canonical.parquet").exists()
    assert (run_dir / "diagnostics" / "manifest.json").exists()  # but it did run


def test_gate_on_images_holds_imageless_new_variants(tmp_path):
    # a new variant (not in the export) whose image isn't on S3 is dropped from BOTH upload files;
    # one with an image stays. No-op when the checker can't verify (returns None upstream).
    from stone_pipeline import catalog
    import csv as _csv
    export = tmp_path / "variants_export.csv"
    with export.open("w", newline="") as h:
        w = _csv.writer(h); w.writerow(["Key", "Name"]); w.writerow(["slab_existing_1", "Existing"])
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        with (tmp_path / fname).open("w", newline="") as h:
            w = _csv.writer(h); w.writerow(["Key", "Name"])
            w.writerow(["slab_existing_1", "Existing"])      # in export -> not gated
            w.writerow(["slab_new_ready", "Ready"])          # new, has image -> kept
            w.writerow(["slab_new_missing", "Missing"])      # new, no image -> dropped
    has_image = lambda k: k == "slab_new_ready"
    held = catalog.gate_on_images(checker=has_image, to_upload=tmp_path, export_file=export)
    assert held == 1
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        keys = {r["Key"] for r in _csv.DictReader((tmp_path / fname).open(encoding="utf-8-sig"))}
        assert "slab_new_missing" not in keys                # imageless new -> held out
        assert {"slab_existing_1", "slab_new_ready"} <= keys # existing + image-ready kept


def test_delete_variant_images_dry_run_counts(tmp_path):
    # dry-run lists the variations/<Key>.png it WOULD delete, removing nothing (no boto3 needed).
    from stone_pipeline import delete_variant_images as d
    counts = d.run(["slab_z_aqua_blue_abc", "slab_z_astoria_def"], apply=False)
    assert counts["would-delete"] == 2 and counts["deleted"] == 0
