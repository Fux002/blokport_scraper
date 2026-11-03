"""An empty folded key is "no usable name", never a shared identity.

match_key drops every non-ASCII letter, so a name written only in a non-Latin script ('黑金砂',
'أسود') folds to "". The index and the engine used to guard only the RAW text, so such a name was
indexed under the key "" and a DIFFERENT non-Latin query looked it up there: with one candidate it
bound as an exact, high-confidence match, merging two different stones.
"""

from __future__ import annotations

import json

from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation

_NON_LATIN = ["黑金砂", "白麻", "أسود", "Серый"]


def test_a_surface_with_an_empty_key_is_never_indexed():
    index = CandidateIndex()
    index.add("c1", "黑金砂", surfaces=["أسود"])
    for table, key in ((index.by_norm, proj.norm("黑金砂")), (index.by_compact, proj.compact("黑金砂")),
                       (index.by_tokenset, proj.tokenset("黑金砂")),
                       (index.by_deprefixed, proj.deprefixed("黑金砂"))):
        assert key not in table, f"empty key {key!r} was indexed"


def test_two_different_non_latin_names_never_bind():
    index = CandidateIndex()
    index.add("c1", "黑金砂", surfaces=[])
    engine = VariationEngine(index, auto_accept=92, review_floor=84)
    for query in _NON_LATIN[1:]:
        match = engine.match(query)
        assert match.cid is None, f"{query!r} bound to {match.cid}"


def test_a_name_with_an_empty_key_routes_to_the_missing_variation_gap():
    stage = match_variation.VariationStage.build(loaders.load_all())
    row = CanonicalRow(src_site="marenostone", surrogate_key="x", raw_name="黑金砂",
                       variety_match_key="黑金砂", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id is None
    assert any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps)


def test_backbone_alias_with_an_empty_key_is_never_indexed(tmp_path):
    # The committed backbones carry Chinese- and Cyrillic-only aliases (1,606 in report 21). Each folds to
    # "", so they all landed in ONE bucket by_norm_alias[""] (about 1,558 entries): a lookup that folds
    # to "" would hand back every one of those varieties.
    from stone_pipeline.reference.loaders.backbone import load_backbone
    path = tmp_path / "backbone.json"
    path.write_text(json.dumps({"posts": [
        {"variant": "Black Galaxy", "stone_type": "Granite", "category": "slab", "aliases": ["黑金砂"]},
        {"variant": "Sesame White", "stone_type": "Granite", "category": "slab", "aliases": ["白麻", "Серый"]},
    ]}), encoding="utf-8")
    backbone = load_backbone(path)
    assert "" not in backbone.by_norm_alias
    assert backbone.lookup_all("黑金砂") == []
    assert [v.variant for v in backbone.lookup_all("Black Galaxy")] == ["Black Galaxy"]
