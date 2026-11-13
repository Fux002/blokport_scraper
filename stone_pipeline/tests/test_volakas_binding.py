"""'Volakas Marble' on the committed production seed (owner, 2026-11-05).

marenostone sells an Iranian white marble as 'Volakas Marble'. The seed gave that spelling to red marble Spider Red,
whose documented origins include Iran, so the origin rung bound the slabs to Spider Red. With the spelling on Volocas
(Persian Volakas, Iran) an Iranian listing binds Volocas; a Greek, Chinese or origin-less one is held as a collision
for review (the trade name is shared by several Greek marbles) and binds nothing, so never Spider Red. The outcome is
asserted, not the candidate list: the engine lists at most three owners of a collision. Reads only committed files.
"""
from __future__ import annotations

import pytest

from stone_pipeline.aliases.stores import seed_of
from stone_pipeline.aliases.surfaces import Stores
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.reference.loaders.backbone import load_backbone
from stone_pipeline.reference.loaders.origins import load_origin_map
from stone_pipeline.reference.loaders.variants import load_variants
from stone_pipeline.tests.test_seed_alias_ownership import ROOT

FORMS = ("slab", "tile", "block")


@pytest.fixture(scope="module")
def engines():
    backbone, origin_map, t = load_backbone(), load_origin_map(), SETTINGS.thresholds
    out = {}
    for form in FORMS:
        # one table, every form
        table = load_variants(ROOT / "from_medusa/production/variants_export_base.csv", Stores(seed=seed_of(backbone)))
        index = build_variation_index(table, backbone, origin_map)
        out[form] = (VariationEngine(index, t.variation_auto_accept, t.variation_review_floor), table)
    return out


@pytest.mark.parametrize("form", FORMS)
def test_an_iranian_volakas_marble_binds_volocas(engines, form):
    eng, table = engines[form]
    m = eng.match("Volakas Marble", block_type="Marble", block_origin="IR")
    assert m.cid is not None and table.by_id[m.cid].key.startswith("marble__volocas_")


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("origin", ["GR", "CN", ""])
def test_any_other_origin_is_a_held_collision_that_binds_nothing(engines, form, origin):
    eng, _table = engines[form]
    m = eng.match("Volakas Marble", block_type="Marble", block_origin=origin)
    assert m.cid is None and m.method.endswith("_collision")
