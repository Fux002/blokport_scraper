"""Curate learns only what the matcher guessed, and every nearest-variety lookup is (name, type) (owner, 2026-11-06).

  * ONE rule decides whether a bind is a guess worth saving as an alias: writeback.is_learned (phonetic, fuzzy,
    fuzzy_origin, looking through the clean_variety_ / operator_type_ wrappers). Curate's alias proposals used a
    second rule, so clean_variety_projection_*, operator_type_projection_* and operator_type_exact binds still
    reached the base as aliases and outlived a later seed correction (#520's intent).
  * The nearest-variety metadata was keyed by NAME only, so for a name held by several types it kept whichever
    type loaded last; a type-less row could be confirmed onto an arbitrary type. It is keyed (name, type) and
    the nearest's type is the matcher candidate's Key type; a type-less row with a multi-type nearest is held.
  * The leaf-suggestion dedup omitted the type: one variety name's card swallowed the other type's.
Hermetic.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.matching import alias_resolver
from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.alias_resolver import AliasResolver
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import load_backbone
from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.stages import curate, match_variation
from stone_pipeline.stages.curate import ImportFile
from stone_pipeline.stages.curate.context import CurationResult
from stone_pipeline.stages.curate.minting import leaf_updates
from stone_pipeline.state import writeback as wb
from stone_pipeline.tests.test_origin_aware_matching import _stage

GRANITE_KEY, MARBLE_KEY = "slab_granite_aqua_blue_G", "slab_marble_aqua_blue_M"


def _imports(varieties) -> dict[str, ImportFile]:
    branches = {}
    for b in ("slab", "block", "tile"):
        imp = ImportFile(branch=b, path=None)
        if b == "slab":
            for key, name, aliases in varieties:
                v = {"Key": key, "Name": name, "Image": "", "Aliases": aliases, "Volume": "",
                     "type": proj.norm(loaders.type_slug_from_key(key))}
                imp.varieties.append(v)
                imp.by_name_type[(proj.norm(name), v["type"])] = v
                imp.by_name[proj.norm(name)] = v
        branches[b] = imp
    return branches


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


# --- one learned-guess rule ------------------------------------------------------------------------------
@pytest.mark.parametrize("method", [
    "fuzzy", "fuzzy_origin", "phonetic", "clean_variety_fuzzy", "operator_type_fuzzy",
    "exact", "exact_name_blocked", "clean_variety_exact", "clean_variety_projection_compact",
    "operator_type_exact", "operator_type_projection_tokenset", "projection_deprefixed", "descriptor_fuzzy",
    "override",
])
def test_curate_proposes_an_alias_exactly_when_the_writeback_would_learn_it(monkeypatch, ref, method):
    monkeypatch.setattr(curate, "load_all_existing",
                        lambda: _imports([("slab_marble_bianco_carrara_C", "Bianco Carrara", "")]))
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Bianco Carara", raw_type="Marble",
                       type_name="Marble", variation_id="C", variation_key="slab_marble_bianco_carrara_C",
                       variation_name="Bianco Carrara", variation_method=method)
    res = curate.build_curation([row], ref)
    proposed = [a["Key"] for a in res.alias_additions["slab"] if a["_status"] == "confirmed"]
    assert proposed == (["slab_marble_bianco_carrara_C"] if wb.is_learned(method) else [])


# --- near_meta is (name, type) ---------------------------------------------------------------------------
def test_near_meta_keeps_every_type_of_a_multi_type_name():
    _resolver, meta = alias_resolver.from_backbones()
    types: dict[str, set[str]] = {}
    seen = set()
    for c in CATEGORIES:
        if c.backbone_path in seen or not c.backbone_path.exists():
            continue
        seen.add(c.backbone_path)
        for vlist in load_backbone(c.backbone_path).by_norm_name.values():
            for v in vlist:
                types.setdefault(proj.norm(v.variant), set()).add(proj.norm(v.stone_type))
    multi = {n: ts for n, ts in types.items() if len(ts) > 1}
    assert multi, "the committed backbones carry multi-type names"
    assert all((n, t) in meta for n, ts in multi.items() for t in ts)


def _aqua_meta():
    return {("aqua blue", "granite"): (["Blue"], ["Aqua Blue Granite"]),
            ("aqua blue", "marble"): (["Blue"], ["Aqua Blue Marble"])}


def _gapped(raw_type: str, nearest_key: str) -> CanonicalRow:
    g = TreeGap(src_site="zucchi", surrogate_key="1", raw_name="Aqua Bleu Slab", normalized_name="aqua bleu",
                suggested_type=raw_type or None, gap_kind=GapKind.missing_variation, nearest_existing="Aqua Blue",
                nearest_score=88.0, nearest_key=nearest_key)
    return CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Aqua Bleu", raw_type=raw_type,
                        variation_method="no_candidate", tree_gaps=[g])


def _aqua(monkeypatch):
    monkeypatch.setattr(curate, "load_all_existing", lambda: _imports(
        [(GRANITE_KEY, "Aqua Blue", "Aqua Blue Granite"), (MARBLE_KEY, "Aqua Blue", "Aqua Blue Marble")]))
    monkeypatch.setattr(curate, "_alias_model", lambda: (AliasResolver(), _aqua_meta()))


def test_a_typed_near_alias_lands_on_the_nearest_candidates_own_type(monkeypatch, ref):
    assert AliasResolver().decide("Aqua Bleu", "", [], "Aqua Blue", "", []).verdict == "alias"
    _aqua(monkeypatch)
    res = curate.build_curation([_gapped("Granite", GRANITE_KEY)], ref)
    assert [a["Key"] for a in res.alias_additions["slab"] if a["_status"] == "confirmed"] == [GRANITE_KEY]


def test_a_type_less_near_alias_of_a_multi_type_name_is_held(monkeypatch, ref):
    _aqua(monkeypatch)
    res = curate.build_curation([_gapped("", GRANITE_KEY)], ref)
    assert not res.alias_additions["slab"], "a type-less row must never be confirmed onto one of the types"
    assert not any(res.new_variants[b] for b in res.new_variants)
    card = next(p for p in res.pending_confirm if p["variant"] == "Aqua Bleu")
    assert card["kind"] == "similar"


def test_the_gap_carries_the_nearest_candidates_key():
    idx = CandidateIndex()
    idx.add("a", "Kashmir Golda", surfaces=[], block_type="granite")
    base = _stage().ref
    stage = match_variation.VariationStage(
        ref=SimpleNamespace(**{**vars(base), "variants": {"slab": SimpleNamespace(
            by_id={"a": SimpleNamespace(key="slab_granite_kashmir_golda_1")})}}),
        engines={"slab": VariationEngine(idx, auto_accept=92, review_floor=84)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Blue",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    gap = next(g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation)
    assert gap.nearest_existing == "Kashmir Golda" and gap.nearest_key == "slab_granite_kashmir_golda_1"


# --- leaf suggestions are per (variety, type) ------------------------------------------------------------
def _leaf_row(stone_type: str, method: str = "exact") -> CanonicalRow:
    r = CanonicalRow(src_site="s", src_url="u", raw_name="Calacatta Gold")
    r.variation_name, r.type_name, r.variation_method = "Calacatta Gold", stone_type, method
    r.add_gap(TreeGap(src_site="s", surrogate_key="k" + stone_type, raw_name="x", normalized_name="calacatta gold",
                      suggested_type=stone_type, suggested_color="Grey", gap_kind=GapKind.missing_leaf_child))
    return r


def test_a_leaf_suggestion_is_kept_per_type():
    res = CurationResult()
    leaf_updates([_leaf_row("Marble"), _leaf_row("Dolomite")], res)
    assert sorted(u["stone_type"] for u in res.backbone_updates) == ["Dolomite", "Marble"]


@pytest.mark.parametrize("method", ["exact", "clean_variety_exact", "clean_variety_projection_compact",
                                    "operator_type_exact", "fuzzy", "clean_variety_fuzzy", "phonetic_origin"])
def test_a_leaf_verdict_uses_the_learned_guess_rule(method):
    res = CurationResult()
    leaf_updates([_leaf_row("Marble", method)], res)
    assert res.backbone_updates[0]["verdict"] == ("verify_match" if wb.is_learned(method) else "likely_real")
