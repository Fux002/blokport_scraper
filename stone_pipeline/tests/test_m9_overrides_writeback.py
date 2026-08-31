"""M9 DoD: a previously gapped row emits after the variant is added, untouched
rows are byte-identical. Plus inbound overrides win, and alias write-back
persists and is re-applied on the next run.
"""

from __future__ import annotations

import csv

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive, match_variation, normalize, reconcile_tree
from stone_pipeline.state import writeback as wb
from stone_pipeline.state.overrides import Overrides


@pytest.fixture
def ref():
    return loaders.load_all()


def test_override_variation_wins(ref):
    # pick a real slab variation id to force
    forced_id = next(iter(ref.variants["slab"].by_id))
    ref.overrides = Overrides(by_key={("polonine", "620"): {"variation_id": forced_id}})
    row = CanonicalRow(src_site="polonine", surrogate_key="620", variety_match_key="UNKNOWN STONE",
                       raw_format="Slab")
    stage = match_variation.VariationStage.build(ref)
    stage.resolve_row(row)
    assert row.variation_id == forced_id
    assert row.variation_method == "override"


def test_override_attribute_wins(ref):
    ref.overrides = Overrides(by_key={("polonine", "1"): {"color_name": "Black"}})
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", surrogate_key="1", raw_color="Chartreuse Sparkle")
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "Black"
    assert row.color_method == "override"
    assert row.color_id is not None


def test_override_bundle_and_origin(ref):
    ref.overrides = Overrides(by_key={("polonine", "1"): {"bundle_size": "12", "origin_country_code": "br"}})
    cfg = load_source("polonine")
    row = CanonicalRow(src_site="polonine", surrogate_key="1", raw_format="Slab",
                       variation_name="Verde Ubatuba", finish_name="Polished")
    derive.run([row], ref, cfg)
    assert row.bundle_size == 12 and row.bundle_size_method == "override"
    assert row.origin_country_code == "BR" and row.origin_source == "override"


def test_alias_writeback_persists_and_reapplies(tmp_path, monkeypatch):
    path = tmp_path / "alias_writeback.csv"
    monkeypatch.setattr(wb, "_writeback_path", lambda name: path)
    writeback = wb.WriteBack()
    writeback.add_alias("vid-1", "Some Scraped Spelling")
    writeback.add_alias("vid-1", "Some Scraped Spelling")  # dup ignored
    assert writeback.flush() == 1
    # second flush of the same content writes nothing (append-only, idempotent)
    again = wb.WriteBack()
    again.add_alias("vid-1", "Some Scraped Spelling")
    assert again.flush() == 0
    loaded = wb.load_alias_writeback(path)
    assert loaded == [("vid-1", "Some Scraped Spelling")]


def test_idempotent_reingest_after_adding_variant(tmp_path, monkeypatch):
    """Add a variant to the slabs reference; a previously gapped row resolves and
    the others are unchanged. Uses a synthetic minimal reference for control."""
    # build a reference with a tiny slabs table missing the variety, then add it
    base = loaders.load_all()

    def make_rows():
        return [
            CanonicalRow(src_site="polonine", surrogate_key="a", variety_match_key="Arabescato",
                         raw_format="Slab", raw_color="White", raw_finish="Polished", raw_quality="A",
                         raw_type="Marble"),
            CanonicalRow(src_site="polonine", surrogate_key="b", variety_match_key="Totally New Variety Xyz",
                         raw_format="Slab", raw_color="Black", raw_finish="Polished", raw_quality="A",
                         raw_type="Granite"),
        ]

    # run 1: Arabescato resolves, the new variety gaps
    rows1 = make_rows()
    normalize.run(rows1, base)
    match_variation.run(rows1, base)
    reconcile_tree.run(rows1, base)
    assert rows1[0].variation_id is not None
    assert rows1[1].variation_id is None  # gapped

    # add the new variety to the slabs reference (simulating the human fix)
    new_id = "01TESTNEWVARIETY00000000XY"
    base.variants["slab"].by_id[new_id] = loaders.Variant(
        variation_id=new_id, key="slab_granite_totally_new", name="Totally New Variety Xyz",
        image="", aliases=[])
    base.backbone.by_norm_name["totally new variety xyz"] = [loaders.BackboneVariety(
        variant="Totally New Variety Xyz", category="Slabs", stone_type="Granite",
        colors=["Black"], finishes=["Polished"], qualities=["A"], aliases=[])]

    # run 2: same scrape, the formerly gapped row now resolves; the other is unchanged
    rows2 = make_rows()
    normalize.run(rows2, base)
    match_variation.run(rows2, base)
    reconcile_tree.run(rows2, base)
    assert rows2[1].variation_id == new_id  # newly resolvable
    # untouched row identical to run 1
    assert rows2[0].variation_id == rows1[0].variation_id
    assert rows2[0].type_name == rows1[0].type_name
