"""A candidate's block_type is the variant Key's type_slug (the sole type authority), normalized -- never
the backbone's display FORM of the type. build_variation_index used to read variety.stone_type; because
backbone.lookup only ever returns a same-type variety, that was byte-identical (verified: 0 mismatches on
the committed seed), but the Key is the authority, so we take it directly. This guards against a regression
back to the backbone display form."""

from __future__ import annotations

from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.reference.loaders import Backbone, BackboneVariety, Variant, VariantTable, _norm

_UUID = "aaaaaaaa-0000-0000-0000-000000000000"


def test_block_type_is_the_key_type_slug_not_the_backbone_display_form():
    key = f"slab_granite_verde_ubatuba_{_UUID}"          # Key slug: granite
    vt = VariantTable(branch="slab")
    vt.by_id["v1"] = Variant(variation_id="v1", key=key, name="Verde Ubatuba", image="", aliases=[])
    bb = Backbone()
    bb.by_norm_name[_norm("Verde Ubatuba")] = [           # backbone DISPLAY form: 'Granite'
        BackboneVariety(variant="Verde Ubatuba", category="slab", stone_type="Granite",
                        colors=["Green"], finishes=[], qualities=[], aliases=[])]
    idx = build_variation_index(vt, bb)
    assert idx.candidates["v1"].block_type == proj.norm("granite")        # from the Key, normalized
    assert idx.candidates["v1"].block_type == proj.norm("Granite")        # display folds to the same


def test_block_type_falls_to_key_when_backbone_lacks_the_variety():
    key = f"slab_quartzite_no_backbone_{_UUID}"
    vt = VariantTable(branch="slab")
    vt.by_id["v2"] = Variant(variation_id="v2", key=key, name="No Backbone", image="", aliases=[])
    idx = build_variation_index(vt, Backbone())          # empty backbone -> block_type from the Key
    assert idx.candidates["v2"].block_type == proj.norm("quartzite")
