"""ONE rule for a statement's colour: it is a documented colour of the variety the statement names, applied at
reference load as a leaf overlay, for a mint and a bind alike. Before, the colour was read only at mint time
(seed_color): a variety minted from a colourless listing kept an empty colour set that no later statement
could fill, so its colourless listings rejected required_id_null forever with no card to act on (prod
2026-09-17, Polonine 'Sparta White' Granite)."""

from __future__ import annotations

from stone_pipeline.config import decisions_store
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders.backbone import Backbone, BackboneVariety


def _rows(*rows):
    base = {"source": "", "spelling_norm": "", "spelling": "", "verdict": "is", "name": None, "stone_type": "",
            "color": None, "origin_iso": None, "widen": 0, "asked_by": ""}
    return [{**base, **r} for r in rows]


def test_a_statements_colour_is_a_documented_colour_of_its_variety_mint_or_bind():
    exists = lambda n, t: (n.lower(), t.lower()) == ("sparta white", "granite")
    dec = Decisions.from_statements(_rows(
        {"source": "polonine", "spelling_norm": "sparta white", "spelling": "SPARTA WHITE",
         "name": "Sparta White", "stone_type": "Granite", "color": "White"},           # a BIND with a colour
        {"source": "", "spelling_norm": "revolution wave", "spelling": "Revolution Wave",
         "stone_type": "Quartzite", "color": "Red", "asked_by": "zucchi"},              # a global MINT with one
        {"source": "varsha", "spelling_norm": "bonsai", "spelling": "Bonsai",
         "name": "Bonsai", "stone_type": "Quartzite"},                                   # no colour: nothing
        {"source": "", "spelling_norm": "junk", "spelling": "Junk", "verdict": "reject"},
    ), exists_as=exists, alias_target=lambda n, t: None)
    assert dec.colour_leaves() == {("sparta white", "granite"): {"color": ["White"]},
                                   ("revolution wave", "quartzite"): {"color": ["Red"]}}


def test_the_overlay_gives_a_colourless_variety_its_documented_colour():
    # the overlay creates an absent record (the durable-membership mechanism) and grows a present one; either
    # way the variety a colourless listing binds to now has a colour for it to inherit
    bb = Backbone()
    bb.by_norm_name["sparta white"] = [BackboneVariety(variant="Sparta White", category="Slabs", stone_type="Granite",
                                                       colors=[], finishes=["Polished"], qualities=["A"], aliases=[])]
    dec = Decisions.from_statements(_rows({"source": "polonine", "spelling_norm": "sparta white",
                                           "spelling": "SPARTA WHITE", "name": "Sparta White",
                                           "stone_type": "Granite", "color": "White"}),
                                    exists_as=lambda n, t: True, alias_target=lambda n, t: None)
    assert bb.apply_leaf_overlay(dec.colour_leaves()) == 1
    assert bb.lookup("Sparta White", stone_type="Granite").colors == ["White"]


def test_reference_load_applies_statement_colours(monkeypatch):
    dec = Decisions.from_statements(_rows({"source": "polonine", "spelling_norm": "sparta white",
                                           "spelling": "SPARTA WHITE", "name": "Sparta White",
                                           "stone_type": "Granite", "color": "White"}),
                                    exists_as=lambda n, t: True, alias_target=lambda n, t: None)
    monkeypatch.setattr(decisions_store, "load_decisions", lambda *a, **k: dec)
    ref = loaders.load_all()
    variety = ref.backbone.lookup("Sparta White", stone_type="Granite")
    assert variety is not None and "White" in variety.colors
