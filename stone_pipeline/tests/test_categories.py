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


def test_pcat_comes_only_from_env_export(monkeypatch):
    # a category's Medusa pcat is sourced ONLY from THIS env's export (attributes.csv -> _ENV_PCATS),
    # never a hardcoded literal; a category absent from the export goes inactive ("").
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings, "_ENV_PCATS", {"Slabs": "pcat_FROM_EXPORT"})
    assert settings._pcat("Slabs") == "pcat_FROM_EXPORT"          # from the env's export
    monkeypatch.setattr(settings, "_ENV_PCATS", {})
    assert settings._pcat("Slabs") == ""                          # absent -> inactive, no literal id
    monkeypatch.setenv("BLOKPORT_CAT_TILES_PCAT", "pcat_OVERRIDE")
    assert settings._pcat("Tiles", "BLOKPORT_CAT_TILES_PCAT") == "pcat_OVERRIDE"  # explicit override only


def test_refresh_picks_up_a_post_import_export_change(tmp_path, monkeypatch):
    # The freeze bug: _ENV_PCATS is read once at import, but produce fetches a fresh attributes.csv AFTER
    # import. If that export changed meaning (here: category rows appear only in the SECOND write, standing
    # in for Blokport's pcat->category label fix landing mid-task), the registry must re-read it -- else
    # every row is category_invalid and new-variety fan-out is silently disabled off the stale snapshot.
    from stone_pipeline.config import settings

    saved = (settings._ENV_PCATS, settings._BY_NAME, settings._BY_LABEL, settings._BY_LABEL_CF)
    monkeypatch.setattr(settings, "_FROM_MEDUSA", tmp_path)
    monkeypatch.delenv("BLOKPORT_CAT_TILES_PCAT", raising=False)  # exercise the file, not the override
    attrs = tmp_path / "attributes.csv"
    try:
        # 1) an export with NO category rows (the stale pre-fix state) -> the registry goes inactive
        attrs.write_text("category,value,sourceid\ncolor,Black,col_x\n", encoding="utf-8")
        settings.refresh_category_pcats()
        assert settings.active_categories() == ()
        assert settings.category("slab").pcat_id == ""            # inactive: nothing can emit

        # 2) the corrected export arrives AFTER import -> refresh reactivates from the live file
        attrs.write_text(
            "category,value,sourceid\n"
            "category,Slabs,pcat_slab\ncategory,Blocks,pcat_block\ncategory,Tiles,pcat_tile\n",
            encoding="utf-8")
        settings.refresh_category_pcats()
        assert settings.category("slab").pcat_id == "pcat_slab"   # re-derived from the fresh export
        assert {c.label for c in settings.active_categories()} == {"Slabs", "Blocks", "Tiles"}
    finally:
        settings._ENV_PCATS, settings._BY_NAME, settings._BY_LABEL, settings._BY_LABEL_CF = saved


def test_refresh_preserves_the_tile_env_override(tmp_path, monkeypatch):
    # the tile pcat has an env-var override; the refresh must re-derive pcat_id the SAME way construction
    # did (override wins over the file), not silently drop it back to the export value.
    from stone_pipeline.config import settings

    saved = (settings._ENV_PCATS, settings._BY_NAME, settings._BY_LABEL, settings._BY_LABEL_CF)
    monkeypatch.setattr(settings, "_FROM_MEDUSA", tmp_path)
    monkeypatch.setenv("BLOKPORT_CAT_TILES_PCAT", "pcat_TILE_OVERRIDE")
    (tmp_path / "attributes.csv").write_text(
        "category,value,sourceid\ncategory,Tiles,pcat_tile_from_file\n", encoding="utf-8")
    try:
        settings.refresh_category_pcats()
        assert settings.category("tile").pcat_id == "pcat_TILE_OVERRIDE"  # override, not the file value
    finally:
        settings._ENV_PCATS, settings._BY_NAME, settings._BY_LABEL, settings._BY_LABEL_CF = saved


def test_owner_ids_are_env_vars_with_prod_guard(monkeypatch):
    # company + sales-channel are operational env vars, never hardcoded: an explicit env var wins;
    # dev falls back to a local default; prod with no env var stays "" (never a dev id).
    from stone_pipeline.config import settings
    monkeypatch.setenv("BLOKPORT_COMPANY_ID", "comp_X")
    assert settings._owner_id("BLOKPORT_COMPANY_ID", "dev_default") == "comp_X"
    monkeypatch.delenv("BLOKPORT_COMPANY_ID", raising=False)
    monkeypatch.setattr(settings, "IS_PRODUCTION", False)
    assert settings._owner_id("BLOKPORT_COMPANY_ID", "dev_default") == "dev_default"
    monkeypatch.setattr(settings, "IS_PRODUCTION", True)
    assert settings._owner_id("BLOKPORT_COMPANY_ID", "dev_default") == ""
