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
    # The committed backbones carry Chinese- and Cyrillic-only aliases. Each folds to "", so they all
    # landed in ONE bucket by_norm_alias[""]: a lookup that folds to "" handed back every such variety.
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


def test_backbone_lookup_of_an_empty_key_finds_nothing_even_after_an_overlay(tmp_path):
    # apply_leaf_overlay CREATES a record for an overlay variety absent from the backbone. An overlay row
    # whose variety folds to "" must never become a record a different non-Latin name can find.
    from stone_pipeline.reference.loaders.backbone import load_backbone
    path = tmp_path / "backbone.json"
    path.write_text(json.dumps({"posts": [
        {"variant": "Black Galaxy", "stone_type": "Granite", "category": "slab"}]}), encoding="utf-8")
    backbone = load_backbone(path)
    backbone.apply_leaf_overlay({("", "granite"): {"color": ["Black"]}})
    assert backbone.lookup_all("白麻") == []
    assert backbone.lookup("Серый", "Granite") is None


def test_a_leaf_decision_on_a_name_with_no_usable_key_is_refused():
    import pytest
    from stone_pipeline.config.decisions_store import set_backbone_leaf_decision
    from stone_pipeline.config.decisions_store._common import InvalidDecision
    with pytest.raises(InvalidDecision):
        set_backbone_leaf_decision("黑金砂", "Granite", "color", "Black", "approve")
    with pytest.raises(InvalidDecision):
        set_backbone_leaf_decision("Black Galaxy", "Granite", "color", "黑", "approve")


def test_minted_membership_skips_a_name_with_no_usable_key(seeded_config_store):
    from stone_pipeline.config.decisions_store import backbone_leaf_overlay, record_minted_membership
    written = record_minted_membership([("黑金砂", "Granite", "color", "Black"),
                                        ("Black Galaxy", "Granite", "color", "Black")])
    assert written == 1
    assert all(variety for variety, _ in backbone_leaf_overlay())


def test_a_statement_on_a_spelling_with_no_usable_key_is_refused():
    import pytest
    from stone_pipeline.config.decisions_store import statements
    from stone_pipeline.config.decisions_store._common import InvalidDecision
    with pytest.raises(InvalidDecision):
        statements._upsert_statement("varsha", "黑金砂", "is", name="Black Galaxy", stone_type="Granite")


def test_curate_never_pools_names_with_no_usable_key_into_one_card(monkeypatch):
    # The curate dedup identity is (type, norm(clean), level): every non-Latin-only name of one type shared
    # the identity (type, "", level), so only the FIRST reached the suspicious-names list and the rest were
    # silently dropped. Each such name must be listed, none minted or held, and every row keeps its gap.
    from stone_pipeline.core.schema import TreeGap
    from stone_pipeline.stages import curate
    monkeypatch.setattr(curate, "load_existing", lambda: curate.ImportFile(path=None))
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    rows = []
    names = ["黑金砂", "白麻", "Серый"]
    for i, name in enumerate(names):
        row = CanonicalRow(src_site="varsha", surrogate_key=f"n{i}", variety_match_key=name, raw_type="Granite")
        row.add_gap(TreeGap(src_site="varsha", surrogate_key=f"n{i}", raw_name=name,
                            gap_kind=GapKind.missing_variation))
        rows.append(row)
    res = curate.build_curation(rows, loaders.load_all())
    assert not res.new_variants
    assert not res.pending_confirm
    assert sorted(s["raw_name"] for s in res.suspicious_names) == sorted(names)
    assert all(any(g.gap_kind == GapKind.missing_variation for g in r.tree_gaps) for r in rows)
