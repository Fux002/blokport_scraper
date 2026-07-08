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
