"""The decision ledger, now backed by config.db (config/decisions_store.py) instead of ephemeral CSVs.

The produce-side facade (stages/decisions.py) keeps the same function names the catalog calls; storage is
the durable, snapshotted config.db. The autouse conftest fixture points BLOKPORT_CONFIG_DB at a per-test
path, so every call here hits an isolated store.
"""

from __future__ import annotations

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.stages import decisions


def test_fresh_store_is_empty_never_raises():
    assert decisions.load_confirm_decisions() == {}
    assert decisions.load_rejected() == set()
    assert decisions.load_alias_decisions() == {}
    assert decisions.load_attribute_ids() == {}
    assert ds.list_pending("variety") == []


def test_review_queue_uniformly_title_cases_display_names():
    # a supplier shouting 'VENATTO BLUE' must show 'Venatto Blue' in the review queue no matter which curate
    # path surfaced it: the code-shaped + alias-review holds used to write raw supplier casing while the mint
    # path title-cased. Now normalized at the one write boundary. Routing (norm-keyed) is unaffected, and a
    # canonical multi-word type is left intact (title_case would mangle 'Semi-Precious Stone').
    decisions.write_confirm_file([
        {"variant": "VENATTO BLUE", "color": "DARK BLUE", "stone_type": "Semi-Precious Stone",
         "reason": "uncertain", "nearest_existing": "ROSAL", "score": "", "model_prob": ""}])
    item = ds.list_pending("variety")[0]
    assert item["variant"] == "Venatto Blue" and item["color"] == "Dark Blue"
    assert item["nearest_existing"] == "Rosal"                  # F1: nearest_existing normalized too
    assert item["stone_type"] == "Semi-Precious Stone"          # canonical type intact, not title-mangled
    ds.set_variety_decision("venatto blue", "reject")           # operator can act by any casing (norm-keyed)
    assert decisions.load_rejected() == {"venatto blue"}


def test_backbone_leaf_queue_title_cases_the_variety_name():
    # F2: the backbone-leaf review's variety name must be cased like the variety queue's variant (it comes
    # from the export, whose casing is unreliable) -- else the same variety reads differently across the two
    # adjacent :4200 review lists. Routing keys on the normalized ref, so it is unaffected.
    decisions.write_backbone_leaf_pending([
        {"variety": "VENATTO BLUE", "stone_type": "Marble", "attribute": "color", "add_value": "Blue",
         "currently_allowed": "White", "match_method": "exact", "match_confidence": "high",
         "verdict": "likely_real", "example_url": ""}])
    item = ds.list_pending("backbone_leaf")[0]
    assert item["variety"] == "Venatto Blue" and item["add_value"] == "Blue"


def test_mint_reject_alias_actions_map_correctly():
    ds.set_variety_decision("Alpha Stone", "mint")
    ds.set_variety_decision("Gamma Stone", "reject")
    ds.set_variety_decision("Bianco Spelling", "alias", alias_of="Bianco Carrara")
    # confirm map is the mint/reject view (alias is NOT in it -- it is routed separately)
    assert decisions.load_confirm_decisions() == {"alpha stone": "yes", "gamma stone": "no"}
    assert decisions.load_rejected() == {"gamma stone"}
    assert decisions.load_alias_decisions() == {"bianco spelling": "Bianco Carrara"}


def test_alias_carries_the_target_type_to_disambiguate_a_multitype_name():
    # A target NAME can exist under several stone types (Black Sea = andesite + soapstone), so an alias
    # must carry WHICH type to route into, else it cannot resolve. mint keeps seed_type as the new type;
    # alias keeps seed_type as the TARGET type; reject keeps none.
    ds.set_variety_decision("Black Turtle", "alias", alias_of="Black Sea", seed_type="Andesite")
    ds.set_variety_decision("Karur White", "mint", seed_type="Granite")
    ds.set_variety_decision("Junk Code", "reject", seed_type="Granite")
    assert decisions.load_alias_decisions() == {"black turtle": "Black Sea"}
    assert decisions.load_alias_types() == {"black turtle": "Andesite"}     # the target type is kept
    assert ds.variety_actions()["karur white"]["seed_type"] == "Granite"    # mint still carries its type
    assert ds.variety_actions()["junk code"]["seed_type"] is None           # reject carries none


def test_re_deciding_a_variety_overwrites():
    ds.set_variety_decision("Flip Stone", "mint")
    assert decisions.load_confirm_decisions() == {"flip stone": "yes"}
    ds.set_variety_decision("Flip Stone", "reject")           # change your mind
    assert decisions.load_confirm_decisions() == {"flip stone": "no"}
    assert decisions.load_rejected() == {"flip stone"}


def test_invalid_decisions_are_rejected_loudly():
    for bad in [("X", "frobnicate", None), ("Y", "alias", None), ("", "mint", None)]:
        try:
            ds.set_variety_decision(*bad)
            assert False, f"expected InvalidDecision for {bad}"
        except ds.InvalidDecision:
            pass


def test_learn_rejects_never_overwrites_an_explicit_decision():
    ds.set_variety_decision("Keeper", "mint")                 # operator said mint
    decisions.save_rejected({"keeper", "junk code"})          # runtime tries to learn a reject
    # the explicit mint survives; only the genuinely new name is learned as a reject
    assert decisions.load_confirm_decisions()["keeper"] == "yes"
    assert "junk code" in decisions.load_rejected()


def test_pending_variety_queue_round_trips_with_current_action():
    ds.set_variety_decision("Alpha Stone", "mint")            # decided between runs
    decisions.write_confirm_file([
        {"confirm": "", "variant": "Delta Stone", "stone_type": "Marble", "color": "white",
         "nearest_existing": "Delta", "score": 0.7, "model_prob": 0.6},
        {"confirm": "", "variant": "Alpha Stone", "stone_type": "Granite"},
    ])
    pending = {p["variant"]: p for p in ds.list_pending("variety")}
    assert set(pending) == {"Delta Stone", "Alpha Stone"}
    assert pending["Delta Stone"]["current_action"] is None           # still undecided
    assert pending["Alpha Stone"]["current_action"] == "mint"         # decided, applies next produce
    # 'confirm' is not persisted (the decision lives as `action`), the informative fields are
    assert pending["Delta Stone"]["nearest_existing"] == "Delta"
    assert "confirm" not in pending["Delta Stone"]


def test_pending_is_fully_replaced_each_write():
    decisions.write_confirm_file([{"confirm": "", "variant": "First"}])
    decisions.write_confirm_file([{"confirm": "", "variant": "Second"}])   # a later produce
    assert [p["variant"] for p in ds.list_pending("variety")] == ["Second"]


def test_pending_ref_collision_collapses_instead_of_crashing():
    # two names that normalize the same (punctuation/case, or same name different type) share a ref; the
    # name-keyed queue keeps ONE entry rather than raising on the (kind, ref) primary key mid-produce.
    decisions.write_confirm_file([
        {"confirm": "", "variant": "Blue-Carara", "stone_type": "Marble"},
        {"confirm": "", "variant": "Blue Carara", "stone_type": "Quartzite"},
    ])
    assert len(ds.list_pending("variety")) == 1
    # attributes collide the same way under one kind
    decisions.write_attributes_to_add([
        {"kind": "finish", "value": "Leathered"}, {"kind": "finish", "value": "leathered"}])
    assert len(ds.list_pending("attribute")) == 1


def test_attribute_ids_round_trip():
    decisions.write_attributes_to_add([
        {"kind": "finish", "value": "Leathered", "count": 12, "suggested_value": "", "action": "", "medusa_id": ""}])
    assert [a["value"] for a in ds.list_pending("attribute")] == ["Leathered"]
    ds.set_attribute_id("finish", "Leathered", "pcol_123")
    assert decisions.load_attribute_ids() == {("finish", "leathered"): ("Leathered", "pcol_123")}
