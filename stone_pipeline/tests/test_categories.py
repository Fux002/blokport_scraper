"""Category registry behaviour, esp. a STANDALONE category (e.g. accessories) that
opts out of the stone-variety model via its flags: not fanned out, not texture
-generated, not matched against stone varieties."""

from __future__ import annotations

import json

from stone_pipeline.config import settings
from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.stages import curate, image_prompts


def _accessory() -> settings.Category:
    return settings.Category(
        "accessory", "accessories", "Accessories", "pcat_ACC_X",
        "accessories_backbone.json", base_image="",
        shares_variety_vocab=False, fan_out=False, mirror_of=None)


def test_one_entry_adds_a_category(monkeypatch):
    monkeypatch.setitem(settings._BY_NAME, "accessory", _accessory())
    assert settings.category("accessory").label == "Accessories"
    assert settings.category_for_key("accessory_sink_x_uuid").name == "accessory"


def test_standalone_not_in_fanout(monkeypatch):
    # fan_out=False: a new stone variety is never created as an accessory
    monkeypatch.setitem(settings._BY_NAME, "accessory", _accessory())
    assert "accessory" not in curate.active_branches()


def test_standalone_not_texture_generated(tmp_path, monkeypatch):
    # base_image="": accessories use real photos via the inbox, not generation
    monkeypatch.setitem(settings._BY_NAME, "accessory", _accessory())
    d = tmp_path / "backbone_additions"
    d.mkdir(parents=True)
    (d / "accessory.json").write_text(json.dumps([
        {"key": "accessory_sink_stone_sink_abc", "variant": "Stone Sink",
         "stone_type": "Sink", "product_backed": True}]), encoding="utf-8")
    out = tmp_path / "prompts.json"
    image_prompts.build(additions_dir=d, out_path=out)
    assert json.loads(out.read_text()) == []  # no base image -> no prompt


def test_standalone_matching_is_held_not_slab_matched(monkeypatch):
    # shares_variety_vocab=False: held with unsupported_category, NOT a stone match
    monkeypatch.setitem(settings._BY_NAME, "accessory", _accessory())
    from stone_pipeline.reference import loaders
    from stone_pipeline.stages.match_variation import VariationStage

    stage = VariationStage.build(loaders.load_all())
    row = CanonicalRow(src_site="x", surrogate_key="1", raw_name="Stone Sink")
    row.format_value = "accessory"
    stage.resolve_row(row)
    assert row.variation_id in (None, "")  # not matched to a stone variety
    assert any(g.gap_kind == GapKind.unsupported_category for g in row.tree_gaps)


def test_standalone_matcher_plugs_in(monkeypatch):
    # registering a matcher for the category is the only stone-pipeline code an
    # accessory vertical adds; match_variation routes to it instead of holding.
    monkeypatch.setitem(settings._BY_NAME, "accessory", _accessory())
    from stone_pipeline.reference import loaders
    from stone_pipeline.stages import match_variation
    from stone_pipeline.stages.match_variation import VariationStage

    def _matcher(row, ref):
        row.variation_id = "acc_123"
        row.variation_method = "accessory_sku"

    monkeypatch.setitem(match_variation.STANDALONE_MATCHERS, "accessory", _matcher)
    stage = VariationStage.build(loaders.load_all())
    row = CanonicalRow(src_site="x", surrogate_key="2", raw_name="Stone Sink")
    row.format_value = "accessory"
    stage.resolve_row(row)
    assert row.variation_id == "acc_123" and row.variation_method == "accessory_sku"
    assert not row.tree_gaps  # matched, not held
