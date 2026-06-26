"""The cross-artifact consistency gate (catalog._consistency_errors) is what guarantees the upload
set is internally coherent -- every product/combination variation exists in the export, and inventory
only targets products that exist in Medusa. These tests cover its set-arithmetic core directly.
"""

from __future__ import annotations

from stone_pipeline.catalog import _consistency_errors


def test_clean_set_passes():
    errors, warnings = _consistency_errors(
        export_ids={"v1", "v2"}, combo_ids={"v1", "v2"}, prod_ids={"v1"},
        inv_skus={"SKU1"}, known_skus={"SKU1", "SKU2"})
    assert errors == []


def test_missing_export_cannot_verify():
    errors, _ = _consistency_errors(set(), {"v1"}, {"v1"}, set(), set())
    assert errors and "cannot verify" in errors[0]


def test_stale_product_and_combination_ids_error():
    errors, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1", "vX"}, prod_ids={"v1", "vY"},
        inv_skus=set(), known_skus=set())
    assert any("combination variation ids are NOT" in e for e in errors)
    assert any("product variation ids are NOT" in e for e in errors)


def test_uncovered_product_is_a_hard_error():
    # a product whose variation has no valid-combination row ships UNPRICEABLE -> hard error (with
    # the finish->Raw fallback every typed variation is covered, so this only fires for a genuinely
    # uncovered variation that must be assigned a type or held, never silently shipped).
    errors, warnings = _consistency_errors(
        export_ids={"v1"}, combo_ids=set(), prod_ids={"v1"}, inv_skus=set(), known_skus=set())
    assert any("NO valid-combination row" in e for e in errors)


def test_orphan_inventory_sku_errors_only_when_export_present():
    # an inventory SKU not in Medusa's product export -> error (don't update a non-existent product)
    errors, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1"}, prod_ids={"v1"},
        inv_skus={"GHOST"}, known_skus={"REAL"})
    assert any("inventory SKUs are NOT in the Medusa product export" in e for e in errors)
    # but with no product export yet (known_skus empty), inventory is not checked
    errors2, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1"}, prod_ids={"v1"},
        inv_skus={"GHOST"}, known_skus=set())
    assert errors2 == []
