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


def test_full_tuple_mismatch_is_a_hard_error():
    # Super White class: the product's variation IS covered at variation level (finish->Raw fallback),
    # so the variation-level gate passes -- but its full (type,variation,finish,colour,quality) tuple
    # was never an allowed combination (served on Quartzite, allowed only under Marble). The tuple gate
    # must catch it.
    quartzite = ("Tq", "v1", "Fraw", "Cwhite", "Qa")
    marble = ("Tm", "v1", "Fraw", "Cwhite", "Qa")
    errors, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1"}, prod_ids={"v1"}, inv_skus=set(), known_skus=set(),
        prod_tuples={quartzite}, combo_tuples={marble})
    assert any("are NOT in valid_combinations" in e for e in errors)


def test_full_tuple_subset_passes():
    tup = ("Tm", "v1", "Fraw", "Cwhite", "Qa")
    errors, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1"}, prod_ids={"v1"}, inv_skus=set(), known_skus=set(),
        prod_tuples={tup}, combo_tuples={tup, ("Tm", "v1", "Fpol", "Cwhite", "Qa")})
    assert errors == []


def test_tuple_gate_silent_when_no_combos_supplied():
    # backward-compat: callers that pass no tuples (default empty) must not trip the tuple check.
    errors, _ = _consistency_errors(
        export_ids={"v1"}, combo_ids={"v1"}, prod_ids={"v1"}, inv_skus=set(), known_skus=set())
    assert errors == []


def _write_csv(path, header, rows):
    import csv
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def test_verify_consistency_roundtrip_pins_column_order(tmp_path, monkeypatch):
    """End-to-end through verify_consistency (not just the pure core): a product whose full tuple IS an
    allowed combination must pass, and one whose tuple is absent must error. This pins the prod_cols<->
    combo_cols column mapping -- a swapped field order would misalign the tuples and flip both results."""
    from types import SimpleNamespace
    from stone_pipeline import catalog
    from stone_pipeline.stages import product_state

    up = tmp_path / "to_upload"; up.mkdir()
    _write_csv(tmp_path / "variants_export.csv", ["Id"], [{"Id": "v1"}])
    _write_csv(up / "2_valid_combinations.csv",
               ["product_category_id", "type_id", "variation_id", "finish_id", "color_id", "quality_id"],
               [{"product_category_id": "pc", "type_id": "Tm", "variation_id": "v1",
                 "finish_id": "Fraw", "color_id": "Cwhite", "quality_id": "Qa"}])
    prod_header = ["STN Type Id", "STN Variation Id", "STN Finish Id", "STN Color Id", "STN Quality Id"]

    def _paths():
        return SimpleNamespace(variants_export_csv=tmp_path / "variants_export.csv", to_upload_dir=up)
    monkeypatch.setattr(catalog, "SETTINGS", SimpleNamespace(paths=_paths()))
    monkeypatch.setattr(product_state, "load_known_products", lambda *a, **k: SimpleNamespace(by_sku={}))

    # matching tuple -> no tuple error
    _write_csv(up / "3_products_all.csv", prod_header,
               [{"STN Type Id": "Tm", "STN Variation Id": "v1", "STN Finish Id": "Fraw",
                 "STN Color Id": "Cwhite", "STN Quality Id": "Qa"}])
    errors, _ = catalog.verify_consistency()
    assert not any("valid_combinations" in e for e in errors), errors

    # orphan tuple (served as Quartzite, allowed only as Marble) -> tuple error
    _write_csv(up / "3_products_all.csv", prod_header,
               [{"STN Type Id": "Tq", "STN Variation Id": "v1", "STN Finish Id": "Fraw",
                 "STN Color Id": "Cwhite", "STN Quality Id": "Qa"}])
    errors, _ = catalog.verify_consistency()
    assert any("are NOT in valid_combinations" in e for e in errors), errors


def test_tuples_missing_column_fails_loud(tmp_path, monkeypatch):
    """A renamed/dropped column in the combinations export must fail loud, not silently no-op the gate."""
    import pytest
    from types import SimpleNamespace
    from stone_pipeline import catalog
    from stone_pipeline.stages import product_state
    up = tmp_path / "to_upload"; up.mkdir()
    _write_csv(tmp_path / "variants_export.csv", ["Id"], [{"Id": "v1"}])
    _write_csv(up / "3_products_all.csv",
               ["STN Type Id", "STN Variation Id", "STN Finish Id", "STN Color Id", "STN Quality Id"],
               [{"STN Type Id": "Tm", "STN Variation Id": "v1", "STN Finish Id": "F",
                 "STN Color Id": "C", "STN Quality Id": "Q"}])
    # combinations file with a RENAMED column (colour_id -> color) must raise
    _write_csv(up / "2_valid_combinations.csv",
               ["product_category_id", "type_id", "variation_id", "finish_id", "color", "quality_id"], [])
    monkeypatch.setattr(catalog, "SETTINGS",
                        SimpleNamespace(paths=SimpleNamespace(variants_export_csv=tmp_path / "variants_export.csv", to_upload_dir=up)))
    monkeypatch.setattr(product_state, "load_known_products", lambda *a, **k: SimpleNamespace(by_sku={}))
    # empty combos file (header only) -> fieldnames present but missing color_id -> fail loud
    _write_csv(up / "2_valid_combinations.csv",
               ["product_category_id", "type_id", "variation_id", "finish_id", "color", "quality_id"],
               [{"product_category_id": "pc", "type_id": "Tm", "variation_id": "v1",
                 "finish_id": "F", "color": "C", "quality_id": "Q"}])
    with pytest.raises(ValueError, match="missing expected column"):
        catalog.verify_consistency()


def test_auto_queue_images_queues_not_generates_without_gen_deps(tmp_path, monkeypatch):
    # The deployed images lack the FLUX/BEN2 stack (fal_client/torch) on purpose. With FAL_KEY set but the
    # deps absent, the produce must QUEUE the texture prompts (not run generators that ImportError) and
    # leave generation to the image_pipeline/GPU pass.
    import importlib.util
    from stone_pipeline import catalog
    from stone_pipeline.stages import image_prompts
    prompts = tmp_path / "prompts.json"
    prompts.write_text('[{"key": "slab_marble_x_1"}]', encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build", lambda: prompts)
    monkeypatch.setenv("FAL_KEY", "present")
    monkeypatch.setattr(importlib.util, "find_spec", lambda m: None)     # no fal_client / torch
    generated = []
    monkeypatch.setattr(catalog, "_generate_queued_images", lambda: generated.append(1) or [])
    assert catalog._auto_queue_images() == 1        # queued
    assert generated == []                          # did NOT attempt inline generation


def test_auto_queue_images_generates_when_deps_present(tmp_path, monkeypatch):
    import importlib.util
    from stone_pipeline import catalog
    from stone_pipeline.stages import image_prompts
    prompts = tmp_path / "prompts.json"
    prompts.write_text('[{"key": "slab_marble_x_1"}]', encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build", lambda: prompts)
    monkeypatch.setenv("FAL_KEY", "present")
    monkeypatch.setattr(importlib.util, "find_spec", lambda m: object())  # deps present -> generate
    generated = []
    monkeypatch.setattr(catalog, "_generate_queued_images", lambda: generated.append(1) or [])
    catalog._auto_queue_images()
    assert generated == [1]                         # inline generation ran
