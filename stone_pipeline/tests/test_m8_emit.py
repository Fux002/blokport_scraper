"""M8 DoD: the emitted CSV validates against the real upload shape and the three
reference maps. This is the same trace that validated v3.1, applied to the
pipeline's own output: every colour/finish/quality/type id resolves via the
attribute map, Type Name equals the Type Id name, variation ids resolve via the
slabs variants file, category is the branch pcat, no required column is empty,
and handles/skus are globally unique.
"""

from __future__ import annotations

import csv
import glob

import pytest

from stone_pipeline.reference import loaders
from stone_pipeline.run import run_source
from stone_pipeline.stages.emit import read_template_columns


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def emitted(tmp_path_factory):
    out = tmp_path_factory.mktemp("outputs")
    run_source("polonine", outputs_dir=out, state_dir=out)
    path = glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0]
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_header_matches_template_exactly(emitted, tmp_path_factory):
    out = tmp_path_factory.mktemp("outputs2")
    run_source("polonine", outputs_dir=out, state_dir=out)
    path = glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0]
    with open(path, newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle))
    assert header == read_template_columns()
    assert len(header) == 55  # 45 base + Product Image 6..15 (see product_image_slots)


def test_emitted_rows_exist(emitted):
    assert len(emitted) >= 10  # the validated polonine rows


def test_every_id_resolves_via_reference(emitted, ref):
    attr_ids = set(ref.attributes.all_ids())
    slab_ids = set(ref.variants_slabs.all_ids())
    for row in emitted:
        for col, vocab in (("STN Color Id", "color"), ("STN Finish Id", "finish"),
                           ("STN Quality Id", "quality"), ("STN Type Id", "type")):
            assert row[col] in attr_ids, f"{col}={row[col]!r} not a known attribute id"
        assert row["STN Variation Id"] in slab_ids
        # Type Name equals the Type Id name
        looked = ref.attributes.resolve_id("type", row["STN Type Name"])
        assert looked and looked[1] == row["STN Type Id"]


def test_category_is_slabs_pcat(emitted):
    pcats = {row["Product Category 1"] for row in emitted}
    assert pcats == {ref_pcat()}


def ref_pcat():
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.backend.cat_slabs_pcat


def test_required_columns_non_empty(emitted):
    required = ["Product Handle", "Product Title", "Product Status", "Product Category 1",
                "Product Sales Channel 1", "Variant Sku", "STN Color Id", "STN Finish Id",
                "STN Quality Id", "STN Variation Id", "STN Type Id", "STN Type Name",
                "STN Company Id", "STN Slug"]
    for row in emitted:
        for col in required:
            assert row[col].strip(), f"required column {col} empty in an emitted row"


def test_handles_and_skus_globally_unique(emitted):
    handles = [row["Product Handle"] for row in emitted]
    skus = [row["Variant Sku"] for row in emitted]
    assert len(handles) == len(set(handles))
    assert len(skus) == len(set(skus))


def test_booleans_lowercase(emitted):
    for row in emitted:
        assert row["Product Discountable"] in ("true", "false")
        assert row["STN Sold In Bundle"] in ("true", "false")
        assert row["STN Popular"] in ("true", "false")


def test_product_image_columns_match_slot_setting():
    # the template must carry exactly product_image_slots numbered Product Image columns, all mapped
    # by emit -- so the column count, the image cap, and the header can never silently drift.
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.stages.emit import COLUMN_MAP
    cols = read_template_columns()
    image_cols = [c for c in cols if c.startswith("Product Image ")]
    assert len(image_cols) == SETTINGS.images.product_image_slots
    assert image_cols == [f"Product Image {i + 1}" for i in range(SETTINGS.images.product_image_slots)]
    assert all(c in COLUMN_MAP for c in image_cols)


def test_product_images_fill_up_to_cap_then_blank():
    # a product with more photos than slots fills every slot; one with fewer leaves the rest blank.
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.config.sources import load_source
    from stone_pipeline.core.schema import CanonicalRow
    from stone_pipeline.stages import images
    from stone_pipeline.stages.emit import COLUMN_MAP
    slots = SETTINGS.images.product_image_slots
    cfg = load_source("polonine")

    many = CanonicalRow(src_site="x")
    many.product_image_keys = [f"u{i}" for i in range(slots + 3)]
    cells = [COLUMN_MAP[f"Product Image {n}"](many, cfg) for n in range(1, slots + 1)]
    assert all(cells) and len(cells) == slots  # every slot filled, extras simply have no column

    few = CanonicalRow(src_site="x")
    few.product_image_keys = ["a", "b"]
    cells = [COLUMN_MAP[f"Product Image {n}"](few, cfg) for n in range(1, slots + 1)]
    assert cells[:2] == ["a", "b"] and cells[2:] == [""] * (slots - 2)


def test_images_slab_slot_cap_tracks_setting():
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.stages import images
    assert images._SLAB_SLOTS == SETTINGS.images.product_image_slots
