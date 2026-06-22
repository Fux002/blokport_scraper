"""Curation loop: emit import-format additions (forgotten aliases + new variants)
for every category, plus synonym-first attribute curation. Files are never
rewritten; deterministic uuid5 keys match the existing format.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind, ReviewFlag, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def test_key_is_deterministic_and_matches_format():
    k1 = curate.gen_key("slab", "Marble", "Foo Bar")
    k2 = curate.gen_key("slab", "Marble", "Foo Bar")
    assert k1 == k2  # deterministic (uuid5), stable across runs
    assert k1.startswith("slab_marble_foo_bar_")
    # structurally a uuid suffix (5 hyphen-separated groups)
    assert len(k1.rsplit("_", 1)[1].split("-")) == 5
    # block variant of the same material gets a different prefix, same name slug
    assert curate.gen_key("block", "Marble", "Foo Bar").startswith("block_marble_foo_bar_")


def test_alias_addition_preserves_and_augments(ref):
    # a confirmed non-exact match proposes adding the scraped spelling as an alias
    # use a spelling not already on the live variant (its real aliases now include
    # "ALPINE"), so the test asserts the ADD path, not idempotent no-op.
    row = CanonicalRow(src_site="polonine", surrogate_key="1",
                       variety_match_key="Alpine Qx9", variation_id="x",
                       variation_name="Alpine", variation_method="fuzzy")
    result = curate.build_curation([row], ref)
    slab = result.alias_additions["slab"]
    assert slab, "expected an alias addition for Alpine"
    entry = next(e for e in slab if e["Name"] == "Alpine")
    assert "Alpine Qx9" in entry["_added"]
    assert entry["_status"] == "confirmed"
    # existing aliases are preserved (not overwritten)
    assert "|" in entry["Aliases"] and len(entry["Aliases"]) > len("ALPINE")
    existing = curate.load_existing("slab").by_name["alpine"]
    assert entry["Key"] == existing["Key"] and entry["Image"] == existing["Image"]  # kept verbatim


def test_exact_match_is_not_proposed_as_alias(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="2",
                       variety_match_key="Arabescato", variation_id="x",
                       variation_name="Arabescato", variation_method="exact")
    result = curate.build_curation([row], ref)
    assert all(e["Name"] != "Arabescato" for e in result.alias_additions["slab"])


def test_new_variant_emitted_for_active_categories(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="3",
                       variety_match_key="Totally Novel Xyz", raw_type="Granite")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="3", raw_name="Totally Novel Xyz",
                        gap_kind=GapKind.missing_variation, nearest_existing="Something", nearest_score=40.0))
    result = curate.build_curation([row], ref)
    # variant created in EVERY active category (uniform catalog): slab, block, tile.
    keys = {}
    for branch in ("slab", "block", "tile"):
        rows = [r for r in result.new_variants[branch] if r["Name"] == "Totally Novel Xyz"]
        assert len(rows) == 1, f"expected new variant in {branch}"
        out = rows[0]
        keys[branch] = out["Key"]
        assert out["Image"].endswith(f"{out['Key']}.png")  # image name IS the Key
        assert "Volume per kg (m³/kg)" in out
        post = next(p for p in result.backbone_new[branch] if p["variant"] == "Totally Novel Xyz")
        assert post["image_file"] == f"{out['Key']}.png"
        assert post["key"] == out["Key"]  # backbone joins to export by this Key
    # the product was observed as a SLAB, so only slab is product-backed and gets an
    # image; the block/tile fan-out copies have no product -> no image generated (no cost).
    posts = {b: next(p for p in result.backbone_new[b] if p["variant"] == "Totally Novel Xyz")
             for b in ("slab", "block", "tile")}
    assert posts["slab"]["product_backed"] is True
    assert posts["block"]["product_backed"] is False
    assert posts["tile"]["product_backed"] is False
    gen = {i["image_filename"] for i in result.images_to_generate}
    assert f"{keys['slab']}.png" in gen           # product-backed -> image
    assert f"{keys['block']}.png" not in gen       # fan-out, no product -> no image
    assert f"{keys['tile']}.png" not in gen        # fan-out, no product -> no image


def test_active_branches_gates_tiles_on_config(monkeypatch):
    import dataclasses
    from stone_pipeline.config import settings
    assert curate.active_branches() == ("slab", "block", "tile")  # tiles active (dev pcat set)
    # clearing the Medusa category id deactivates the category, no code change
    tile = dataclasses.replace(settings._BY_NAME["tile"], pcat_id="")
    monkeypatch.setitem(settings._BY_NAME, "tile", tile)
    assert curate.active_branches() == ("slab", "block")


def test_borderline_gap_becomes_alias_candidate_not_new_variant(ref):
    # a gap whose nearest existing variety scores high is proposed as an ALIAS of
    # that variety, not a new variant (avoids near-duplicate variants)
    nearest = next(iter(ref.variants_slabs.by_id.values())).name
    row = CanonicalRow(src_site="polonine", surrogate_key="ac1",
                       variety_match_key="Suppliers Rebrand Xyz", raw_type="Marble")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="ac1", raw_name="Suppliers Rebrand Xyz",
                        gap_kind=GapKind.missing_variation, nearest_existing=nearest, nearest_score=82.0))
    result = curate.build_curation([row], ref)
    # not proposed as a new variant
    assert all(r["Name"] != "Suppliers Rebrand Xyz" for r in result.new_variants["slab"])
    # proposed as an alias of the nearest existing variety, needs_review
    target = next(r for r in result.alias_additions["slab"] if r["Name"] == nearest)
    assert "Suppliers Rebrand Xyz" in target["_added"]
    assert target["_status"] == "needs_review"


def test_new_variant_emits_backbone_entry_per_active_category(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="bb1",
                       variety_match_key="Brand New Stone", raw_type="Granite",
                       color_name="Blue", quality_name="A", finish_name="Polished")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="bb1", raw_name="Brand New Stone",
                        gap_kind=GapKind.missing_variation, nearest_score=30.0))
    result = curate.build_curation([row], ref)
    for branch, cat in (("slab", "Slabs"), ("block", "Blocks"), ("tile", "Tiles")):
        post = next(p for p in result.backbone_new[branch] if p["variant"] == "Brand New Stone")
        assert post["category"] == cat
        assert post["stone_type"] == "Granite"
        assert post["color"] == ["Blue"]
        assert "Polished" in post["finishes"]
        assert post["qualities"] == ["A"]
        assert post["key"]  # the deterministic join key; Medusa assigns the Id on import


def test_backbone_update_flags_missing_value_with_verdict(ref):
    variety = ref.backbone.lookup("Verde Ubatuba")
    bad_color = "Pink" if "Pink" not in variety.colors else "Orange"
    row = CanonicalRow(src_site="polonine", surrogate_key="bu1",
                       variation_id="x", variation_name="Verde Ubatuba", variation_method="exact",
                       variation_confidence="high", type_name=variety.stone_type,
                       color_name=bad_color, finish_name=variety.finishes[0],
                       quality_name=variety.qualities[0])
    from stone_pipeline.stages import reconcile_tree
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    result = curate.build_curation([row], ref)
    upd = next(u for u in result.backbone_updates if u["variety"] == "Verde Ubatuba")
    assert upd["attribute"] == "color"
    assert upd["add_value"] == bad_color
    assert upd["verdict"] == "likely_real"  # exact match -> trust the missing-value signal


def test_attribute_curation_suggests_synonym(ref):
    row = CanonicalRow(src_site="zucchi", surrogate_key="4")
    row.add_flag(ReviewFlag(field="type", code=FlagCode.attr_unresolved, raw_value="Semiprecious"))
    attr = curate.build_attribute_curation([row], ref)
    entry = next(a for a in attr if a["raw_value"] == "Semiprecious")
    assert entry["suggested_value"] == "Semi-Precious Stone"
    assert entry["recommended_action"] in ("synonym", "synonym?")


def test_curation_does_not_modify_reference_files(ref, tmp_path):
    # build_curation only READS the immutable export; it must not write to catalog_source/from_medusa
    import os
    ref_path = str(curate.SETTINGS.paths.export_file)
    before = {ref_path: os.path.getmtime(ref_path)} if os.path.exists(ref_path) else {}
    curate.build_curation([], ref)
    after = {p: os.path.getmtime(p) for p in before}
    assert before == after
