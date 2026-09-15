"""A minted variety's backbone membership is durable and cannot drift.

Root cause fixed here: `catalog_source/backbone_additions/*.json` is overwritten every produce with only
that run's mints (it must stay per-run: the texture queue reads it), so a variety minted at run N drops out
of it at run N+1 while it stays in the base -> `ref.backbone.lookup` returns None -> `no_backbone_record`
and its products skip on color_id with nothing to review. The fix puts the membership in the durable config.db
`backbone_leaf_decision` overlay (same lifecycle as the mint statement: snapshotted, restored, dropped by a
pristine reset), and `apply_leaf_overlay` now CREATES a record for a variety absent from every backbone file.

Pure tests: the autouse conftest fixture isolates config.db.
"""

from __future__ import annotations

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.reference.loaders import Backbone, BackboneVariety, _norm
from stone_pipeline.stages import decisions


def _variety(name="Tiger Black", stone_type="Granite", colors=("Black",), finishes=("Polished",), quals=("A",)):
    return BackboneVariety(variant=name, category="Slabs", stone_type=stone_type,
                           colors=list(colors), finishes=list(finishes), qualities=list(quals), aliases=[])


def _backbone(*varieties) -> Backbone:
    bb = Backbone()
    for v in varieties:
        bb.by_norm_name.setdefault(_norm(v.variant), []).append(v)
    return bb


# -- the keystone: the overlay can CREATE an absent variety ----------------------------------------------

def test_apply_leaf_overlay_creates_a_variety_absent_from_every_file():
    bb = _backbone()                                   # empty backbone (the drift: variety in base, not here)
    overlay = {("alpine luxe", "granite"): {"color": ["White"], "finish": ["Polished"], "quality": ["A"]}}
    added = bb.apply_leaf_overlay(overlay)
    assert added == 3
    v = bb.lookup("Alpine Luxe", "Granite")
    assert v is not None, "the overlay must create the record so reconcile no longer flags no_backbone_record"
    assert v.colors == ["White"] and v.finishes == ["Polished"] and v.qualities == ["A"]
    assert v.stone_type == "Granite"


def test_overlay_created_variety_is_type_scoped():
    bb = _backbone()
    bb.apply_leaf_overlay({("bonsai", "quartzite"): {"color": ["Green"]}})
    assert bb.lookup("Bonsai", "Quartzite") is not None
    assert bb.lookup("Bonsai", "Crystal") is None      # a different type is a different variety, never lent


def test_apply_leaf_overlay_still_widens_an_existing_variety():
    bb = _backbone(_variety(colors=["Black"], quals=["A"]))
    added = bb.apply_leaf_overlay({("tiger black", "granite"): {"quality": ["B"], "color": ["Black"]}})
    assert added == 1                                   # only B is new; Black already allowed (idempotent)
    v = bb.lookup("Tiger Black", "Granite")
    assert set(v.qualities) == {"A", "B"} and v.colors == ["Black"]
    assert len(bb.by_norm_name["tiger black"]) == 1     # widened in place, not duplicated


def test_overlay_ignores_a_type_less_key():
    bb = _backbone()
    assert bb.apply_leaf_overlay({("nameless", ""): {"color": ["White"]}}) == 0
    assert bb.lookup("Nameless") is None                # never create an untyped record


# -- the durable write: a mint's membership round-trips through config.db ---------------------------------

def test_record_minted_membership_becomes_an_approved_overlay(config_db=None):
    ds.record_minted_membership([("Alpine Luxe", "Granite", "color", "White"),
                                 ("Alpine Luxe", "Granite", "finish", "Polished")])
    overlay = ds.backbone_leaf_overlay()
    assert overlay[("alpine luxe", "granite")] == {"color": ["White"], "finish": ["Polished"]}
    # and it feeds a real backbone into existence
    bb = _backbone()
    bb.apply_leaf_overlay(overlay)
    assert bb.lookup("Alpine Luxe", "Granite").colors == ["White"]


def test_record_minted_membership_skips_a_bad_attribute_or_empty_value():
    n = ds.record_minted_membership([("V", "Granite", "not_an_attr", "X"),
                                     ("V", "Granite", "color", ""), ("V", "Granite", "color", "Red")])
    assert n == 1                                       # only the valid (color, Red) row
    assert ds.backbone_leaf_overlay()[("v", "granite")] == {"color": ["Red"]}


def test_write_minted_membership_from_curate_posts():
    posts = {"slab": [{"variant": "Everest Quartzite", "stone_type": "Quartzite",
                       "color": ["Grey"], "finishes": ["Honed", "Polished"], "qualities": ["A"]}],
             "block": [{"variant": "Everest Quartzite", "stone_type": "Quartzite",   # same variety, dedup
                        "color": ["Grey"], "finishes": ["Honed"], "qualities": ["A"]}]}
    written = decisions.write_minted_membership(posts)
    assert written == 4                                 # Grey, Honed, Polished, A -- once each
    overlay = ds.backbone_leaf_overlay()[("everest quartzite", "quartzite")]
    assert set(overlay["color"]) == {"Grey"} and set(overlay["finish"]) == {"Honed", "Polished"}


# -- lifecycle: unmint drops the membership beside the statement ------------------------------------------

def test_clear_for_variety_drops_the_membership_it_rode_beside():
    ds.record_minted_membership([("Alpine Luxe", "Granite", "color", "White")])
    assert ds.backbone_leaf_overlay()                   # present
    ds.clear_for_variety("Alpine Luxe", "granite")      # unmint's clear
    assert ds.backbone_leaf_overlay() == {}             # membership gone with the statement, no stray record


# -- the heal: an already-drifted variety gets durable membership (statement colour first) ----------------

def _drifted_row(name, stone_type, color=None, finish=None, quality=None):
    from stone_pipeline.core.schema import CanonicalRow, ReviewFlag, FlagCode
    return CanonicalRow(src_site="varsha", surrogate_key="s1", variation_name=name, type_name=stone_type,
                        color_name=color, finish_name=finish, quality_name=quality,
                        review_flags=[ReviewFlag(field="variation", code=FlagCode.attr_unresolved,
                                                 method="no_backbone_record", raw_value=name)])


def test_heal_prefers_the_statement_seed_colour_over_observed():
    # operator minted Alpine Luxe as White; the scraped product carries a DIFFERENT colour -> White wins
    ds.decide("varsha", "Alpine Luxe", "Alpine Luxe", "Granite", color="White",
              exists_as=lambda n, t: False, alias_target=lambda n, t: None)
    written = decisions.heal_drifted_membership([_drifted_row("Alpine Luxe", "Granite", color="Grey")])
    assert written == 1
    assert ds.backbone_leaf_overlay()[("alpine luxe", "granite")] == {"color": ["White"]}   # seed, not Grey


def test_heal_falls_back_to_observed_when_no_seed_colour():
    row = _drifted_row("Everest Quartzite", "Quartzite", color="Grey", finish="Honed", quality="A")
    decisions.heal_drifted_membership([row])
    got = ds.backbone_leaf_overlay()[("everest quartzite", "quartzite")]
    assert got == {"color": ["Grey"], "finish": ["Honed"], "quality": ["A"]}


def test_heal_leaves_a_colourless_variety_to_surface_honestly():
    # no seed colour, no observed colour -> no colour is guessed (it surfaces as needs-a-colour)
    decisions.heal_drifted_membership([_drifted_row("Smoky Quartzite", "Quartzite", finish="Polished")])
    got = ds.backbone_leaf_overlay().get(("smoky quartzite", "quartzite"), {})
    assert "color" not in got and got.get("finish") == ["Polished"]


def test_heal_ignores_rows_without_the_drift_flag():
    from stone_pipeline.core.schema import CanonicalRow
    ok = CanonicalRow(src_site="varsha", surrogate_key="s2", variation_name="Fine", type_name="Granite",
                      color_name="Black", review_flags=[])
    assert decisions.heal_drifted_membership([ok]) == 0
    assert ds.backbone_leaf_overlay() == {}
