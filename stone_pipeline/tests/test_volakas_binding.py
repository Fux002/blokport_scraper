"""'Volakas Marble' on the committed production seed (owner, 2026-11-05).

marenostone sells an Iranian white marble as 'Volakas Marble'. The seed gave that spelling to red marble Spider Red,
whose documented origins include Iran, so the origin rung bound the slabs to Spider Red. With the spelling on Volocas
(Persian Volakas, Iran) an Iranian listing binds Volocas; a Greek or Chinese one stays a collision for review (the
trade name is shared by several Greek marbles), and Spider Red is never chosen. Reads only committed files.
"""
from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.reference.loaders.backbone import load_backbone
from stone_pipeline.reference.loaders.origins import load_origin_map
from stone_pipeline.reference.loaders.variants import load_variants
from stone_pipeline.tests.test_seed_alias_ownership import ROOT


@pytest.fixture(scope="module")
def engine():
    table = load_variants(ROOT / "from_medusa/production/variants_export_base.csv", "slab", key_prefix="slab_")
    index = build_variation_index(table, load_backbone(), load_origin_map())
    t = SETTINGS.thresholds
    return VariationEngine(index, t.variation_auto_accept, t.variation_review_floor), table


def _name(table, cid):
    return table.by_id[cid].name if cid else None


def test_an_iranian_volakas_marble_binds_volocas(engine):
    eng, table = engine
    m = eng.match("Volakas Marble", block_type="Marble", block_origin="IR")
    assert _name(table, m.cid) == "Volocas"


@pytest.mark.parametrize("origin", ["GR", "CN", ""])
def test_any_other_origin_is_held_and_never_spider_red(engine, origin):
    eng, table = engine
    m = eng.match("Volakas Marble", block_type="Marble", block_origin=origin)
    assert m.cid is None and m.method.endswith("_collision")
    assert "Spider Red" not in {name for _cid, name, _score in m.candidates}
