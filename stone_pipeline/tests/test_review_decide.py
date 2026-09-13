"""ONE statement resolves every review case. The operator says what a vendor's product IS -- name, type,
colour, origin -- and the backend derives the outcome from what exists: bind to an existing variety (scoped
alias + the vendor's origin), or mint it (global variety, the vendor's spelling bound through a scoped alias
when renamed), optionally widening the variety's documented origins. Clearing undoes exactly what one
statement stored. The resolved list shows the last produce's outcome per product with the standing decision
overlaid. Self-contained: stub variety lookup, tmp config.db, a hand-written canonical parquet."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from stone_pipeline.config import decisions_store, resolved, server, store, varieties
from stone_pipeline.reference.loaders import _norm
from stone_pipeline.tests import _decision_views as views

EXISTING = {("golden lightning", "granite"), ("azul white", "onyx"), ("azul white", "quartzite"),
            ("brown granite", "granite")}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "config_db_path", lambda: tmp_path / "config.db")
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: (_norm(n), _norm(t)) in EXISTING)
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)   # no alias resolution unless a test opts in
    monkeypatch.setattr(server, "_type_vocab", lambda: {"granite": "Granite", "onyx": "Onyx", "quartzite": "Quartzite"})
    yield


def _exists(n, t):
    return (_norm(n), _norm(t)) in EXISTING


def test_existing_variety_binds_for_the_vendor_with_its_origin():
    out = decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite",
                                 origin="IR", exists_as=_exists)
    assert out["result"] == "bound"
    assert views.scoped(exists_as=_exists)[("marenostone", _norm("Amazon Green Granite"))] == ("Golden Lightning", "Granite")
    assert views.origins(exists_as=_exists)[("marenostone", "golden lightning", "granite")] == "IR"
    assert views.actions(exists_as=_exists) == {}                       # nothing minted
    assert decisions_store.variety_origins() == {}                       # documented origins untouched


def test_unknown_variety_mints_globally_and_binds_the_vendor_spelling():
    out = decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White Persian", "Onyx",
                                 color="White", origin="IR", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    dec = views.actions(exists_as=_exists)[("", _norm("Azul White Quartzite"))]
    assert dec["action"] == "mint" and dec["seed_name"] == "Azul White Persian"
    assert dec["seed_type"] == "Onyx" and dec["seed_color"] == "White" and dec["seed_country"] == "IR"
    assert dec["source"] == "" and dec["asked_by"] == "marenostone"          # the first mint is the spelling's meaning
    assert views.seed_scopes(exists_as=_exists) == {} and views.scoped(exists_as=_exists) == {}
    assert views.origins(exists_as=_exists)[("marenostone", "azul white persian", "onyx")] == "IR"


def test_name_that_is_an_existing_alias_binds_to_its_variety_not_a_mint(monkeypatch):
    # an origin card names 'Artemis' (already an alias of 'Andes' Quartzite): the statement must bind the
    # vendor's spelling to Andes and record Andes' origin, never mint a duplicate variety 'Artemis'
    _andes = lambda n, t: _exists(n, t) or (_norm(n), _norm(t)) == ("andes", "quartzite")   # the alias's owner exists
    monkeypatch.setattr(varieties, "alias_target",
                        lambda n, t: "Andes" if (_norm(n), _norm(t)) == ("artemis", "quartzite") else None)
    out = decisions_store.decide("polonine", "ARTEMIS", "Artemis", "Quartzite", origin="BR", widen=True,
                                 exists_as=_andes)
    assert out["result"] == "bound" and out["resolved_alias"] is True and out["name"] == "Andes"
    assert views.scoped(exists_as=_andes)[("polonine", _norm("ARTEMIS"))] == ("Andes", "Quartzite")
    assert views.origins(exists_as=_andes)[("polonine", "andes", "quartzite")] == "BR"
    assert views.actions(exists_as=_andes) == {}                       # nothing minted -- no duplicate Artemis
    assert views.widen(exists_as=_andes)[("polonine", "andes", "quartzite")] == "BR"   # widen keyed to the target
    assert decisions_store.variety_origins() == {}                                        # the list is unioned at load, never rewritten


def test_same_name_mint_is_not_a_rename():
    decisions_store.decide("varsha", "Bonsai", "Bonsai", "Quartzite", exists_as=_exists)
    dec = views.actions(exists_as=_exists)[("", _norm("Bonsai"))]
    assert dec["action"] == "mint" and dec["seed_name"] is None
    assert views.scoped(exists_as=_exists) == {}
    assert views.seed_scopes(exists_as=_exists) == {}


def test_widen_is_recorded_on_the_decision_and_never_rewrites_the_stones_list():
    from stone_pipeline.reference.loaders import OriginMap, OriginRule
    decisions_store.decide("marenostone", "Golden Lightning", "Golden Lightning", "Granite",
                           origin="IR", widen=True, exists_as=_exists)
    assert views.origins(exists_as=_exists)[("marenostone", "golden lightning", "granite")] == "IR"
    assert views.widen(exists_as=_exists) == {("marenostone", "golden lightning", "granite"): "IR"}
    assert decisions_store.variety_origins() == {}                     # the explicit list edit is untouched
    # at load the widened country is ADDED to what the map documents, never replacing it
    m = OriginMap(rules=[OriginRule(variety="Golden Lightning", country_iso="BR,CN", city="", county="", stone_type="Granite")])
    assert m.widen(views.widen(exists_as=_exists)) == 1
    assert m.exact("Golden Lightning", "Granite").countries == ["BR", "CN", "IR"]
    assert m.widen(views.widen(exists_as=_exists)) == 0                 # idempotent
    # a stone the map does not document yet gets its first documented origin
    decisions_store.decide("zucchi", "Andes", "Andes", "Quartzite", origin="BR", widen=True, exists_as=_exists)
    assert m.widen(views.widen(exists_as=_exists)) == 1 and m.exact("Andes", "Quartzite").countries == ["BR"]


def test_redeciding_overwrites_and_clearing_removes_exactly_that_vendors_decisions():
    decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White", "Quartzite", origin="BR",
                           exists_as=_exists)
    decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White", "Onyx", origin="IR",
                           exists_as=_exists)
    assert views.scoped(exists_as=_exists)[("marenostone", _norm("Azul White Quartzite"))] == ("Azul White", "Onyx")
    # another vendor's decision on the same spelling is independent
    decisions_store.decide("zucchi", "Azul White Quartzite", "Azul White", "Quartzite", origin="BR",
                           exists_as=_exists)
    dropped = {"statements": decisions_store.clear("marenostone", "Azul White Quartzite")}
    assert dropped == {"statements": 1}
    assert ("marenostone", _norm("Azul White Quartzite")) not in views.scoped(exists_as=_exists)
    assert views.scoped(exists_as=_exists)[("zucchi", _norm("Azul White Quartzite"))] == ("Azul White", "Quartzite")
    assert views.origins(exists_as=_exists) == {("zucchi", "azul white", "quartzite"): "BR"}


def test_clear_leaves_a_global_mint_alone():
    views.mint("Totally New", stone_type="Granite")      # made for every vendor
    dropped = {"statements": decisions_store.clear("marenostone", "Totally New")}
    assert dropped["statements"] == 0
    assert views.actions(exists_as=_exists)[("", _norm("Totally New"))]["action"] == "mint"


def test_missing_fields_store_nothing():
    with pytest.raises(decisions_store.InvalidDecision):
        decisions_store.decide("marenostone", "Azul White", "", "Onyx", exists_as=_exists)
    assert views.scoped(exists_as=_exists) == {} and views.actions(exists_as=_exists) == {}


# --- the API ---------------------------------------------------------------------------------------------

def test_put_decide_validates_then_stores():
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Amazon Green Granite",
                                  "name": "Golden Lightning", "type": "granite", "origin": "Iran"})
    assert code == 200 and body["result"] == "bound" and body["origin"] == "IR"
    code, _ = server.dispatch("PUT", ["review", "decide"],
                              {"source": "marenostone", "scraped": "X", "name": "Y", "type": "Wood"})
    assert code == 400
    code, _ = server.dispatch("PUT", ["review", "decide"],
                              {"source": "marenostone", "scraped": "X", "name": "Y", "type": "Onyx",
                               "origin": "Notacountry"})
    assert code == 400
    assert ("", _norm("X")) not in views.actions(exists_as=_exists)


def test_put_decide_applies_one_statement_to_every_spelling_on_a_card():
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": ["Amazon Green Granite", "Amazon Green Granite Slab"],
                                  "name": "Golden Lightning", "type": "Granite", "origin": "IR"})
    assert code == 200 and body["decided"] == 2
    keys = {k for k in views.scoped(exists_as=_exists) if k[0] == "marenostone"}
    assert keys == {("marenostone", _norm("Amazon Green Granite")), ("marenostone", _norm("Amazon Green Granite Slab"))}


def test_put_decide_refuses_a_retired_variety(monkeypatch):
    from stone_pipeline.stages import decisions as stage_decisions
    from stone_pipeline.stages.curate import active_branches, gen_key
    key = gen_key(active_branches()[0], "Onyx", "Old Stone")
    monkeypatch.setattr(stage_decisions, "load_retired", lambda: {key})
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Old Stone", "name": "Old Stone", "type": "Onyx"})
    assert code == 409 and body["retired"]
    assert views.actions(exists_as=_exists) == {}


def test_delete_decide_clears():
    server.dispatch("PUT", ["review", "decide"], {"source": "marenostone", "scraped": "Amazon Green Granite",
                                                  "name": "Golden Lightning", "type": "Granite", "origin": "IR"})
    code, body = server.dispatch("DELETE", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Amazon Green Granite"})
    assert code == 200 and body["cleared"] == [{"statements": 1}]
    assert views.scoped(exists_as=_exists) == {}


def test_variety_card_carries_the_spellings_a_statement_is_keyed_on():
    from stone_pipeline.stages import decisions as stage_decisions
    pending = [{"variant": "Amazon Green", "reason": "r", "stone_type": "Granite", "src": "marenostone",
                "scraped": "Amazon Green Granite", "sources": ["marenostone"]},
               {"variant": "Amazon Green", "reason": "r", "stone_type": "Granite", "src": "marenostone",
                "scraped": "Amazon Green Granite Slab", "sources": ["marenostone"]}]
    assert stage_decisions.write_confirm_file(pending) == 1
    card = decisions_store.list_pending("variety")[0]
    assert card["scraped"] == "Amazon Green Granite"
    assert card["spellings"] == ["Amazon Green Granite", "Amazon Green Granite Slab"]


# --- the resolved list -------------------------------------------------------------------------------------

def _canonical(tmp_path: Path) -> Path:
    run = tmp_path / "outputs" / "marenostone_20260907_000000" / "diagnostics"
    run.mkdir(parents=True)
    pl.DataFrame({
        "src_site": ["marenostone", "marenostone", "marenostone"], "surrogate_key": ["1", "2", "3"],
        "raw_name": ["Azul White Quartzite Slab", "Volakas Marble Slab", "Amazon Green Granite Slab"],
        "variety_match_key": ["Azul White Quartzite", "Volakas Marble", "Amazon Green Granite"],
        "variation_key": ["slab_quartzite_azul_white_x", "slab_marble_spider_red_x", None],
        "variation_name": ["Azul White", "Spider Red", None],
        "variation_method": ["clean_variety_exact", "exact_origin", "exact_blocked_collision"],
        "type_name": ["Quartzite", "Marble", "Granite"],
        "type_method": ["variety_authoritative", "variety_authoritative", "name_explicit"],
        "color_name": ["White", "White", "Green"], "origin_country_code": [None, "IR", None],
        "origin_source": ["origin_needs_confirmation", "vendor_origin", "supplier_default"],
        "src_url": ["https://m/azul", "https://m/volakas", "https://m/amazon"],
    }).write_parquet(run / "canonical.parquet")
    return tmp_path / "outputs"


def test_resolved_lists_the_last_produce_with_the_standing_decision(tmp_path):
    outputs = _canonical(tmp_path)
    rows = resolved.list_resolved(outputs_dir=outputs)
    # the unbound Amazon Green (a pending card) is NOT here: the two lists never show the same product
    assert [r["scraped"] for r in rows] == ["Azul White Quartzite", "Volakas Marble"]
    assert rows[0]["decision"] is None and rows[0]["resolved_by"]["origin"] == "origin_needs_confirmation"
    decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White", "Onyx", origin="IR", exists_as=_exists)
    rows = resolved.list_resolved(outputs_dir=outputs, decided=True)
    assert [r["scraped"] for r in rows] == ["Azul White Quartzite"]
    assert rows[0]["decision"] == {"kind": "bound", "name": "Azul White", "stone_type": "Onyx", "origin": "IR"}
    assert rows[0]["name"] == "Azul White" and rows[0]["stone_type"] == "Quartzite"   # last produce, not yet re-run
    assert resolved.list_resolved(outputs_dir=outputs, decided=False)[0]["scraped"] == "Volakas Marble"
    assert resolved.list_resolved(source="zucchi", outputs_dir=outputs) == []


def test_resolved_is_empty_without_a_produce(tmp_path):
    assert resolved.list_resolved(outputs_dir=tmp_path / "nothing") == []


# --- one list, one card shape -------------------------------------------------------------------------------

def test_variants_list_includes_origin_confirmations_as_origin_cards():
    from stone_pipeline.stages import decisions as stage_decisions
    stage_decisions.write_confirm_file([{"variant": "Amazon Green", "reason": "r", "stone_type": "Granite",
                                         "src": "marenostone", "scraped": "Amazon Green Granite", "kind": "collision",
                                         "sources": ["marenostone"]}])
    decisions_store.replace_pending("origin", [{
        "ref": "marenostone|azul white|quartzite", "sources": ["marenostone"],
        "payload": {"source": "marenostone", "variety": "Azul White", "stone_type": "Quartzite",
                    "scraped": "Azul White Quartzite", "map_country": "BR", "vendor_origin": "IR",
                    "src_url": "https://m/azul", "image": "https://m/azul.jpg"}}])
    code, body = server.dispatch("GET", ["review", "variants"], None)
    assert code == 200
    kinds = {c["variant"]: c["kind"] for c in body["variants"]}
    assert kinds == {"Amazon Green": "collision", "Azul White": "origin"}
    origin = next(c for c in body["variants"] if c["kind"] == "origin")
    assert origin["scraped"] == "Azul White Quartzite" and origin["spellings"] == ["Azul White Quartzite"]
    assert origin["origin"] == "IR" and origin["stone_type"] == "Quartzite" and "BR" in origin["reason"]
    # the same statement resolves it
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": origin["src"], "scraped": origin["spellings"],
                                  "name": "Azul White", "type": "Onyx", "origin": "IR"})
    assert code == 200 and body["result"] == "bound"


# --- edge cases: a global mint is never narrowed; a card shared by two vendors decides each under its own ---

def test_a_vendor_naming_a_different_stone_from_a_global_mint_gets_its_own_level():
    views.mint("Totally New", stone_type="Granite")          # every vendor
    out = decisions_store.decide("marenostone", "Totally New", "Totally New Stone", "Granite", exists_as=_exists)
    assert out["level"] == "vendor"
    assert views.actions(exists_as=_exists)[("", _norm("Totally New"))]["seed_name"] is None     # the global is untouched
    assert views.actions(exists_as=_exists)[("marenostone", _norm("Totally New"))]["seed_name"] == "Totally New Stone"
    assert views.scoped(exists_as=_exists)[("marenostone", _norm("Totally New"))] == ("Totally New Stone", "Granite")


def test_a_card_shared_by_two_vendors_decides_each_listing_under_its_own_vendor():
    from stone_pipeline.stages import decisions as stage_decisions
    stage_decisions.write_confirm_file([
        {"variant": "Via Lactea", "reason": "r", "stone_type": "Granite", "src": "polonine",
         "scraped": "VIA LACTEA", "sources": ["polonine", "zucchi"]},
        {"variant": "Via Lactea", "reason": "r", "stone_type": "Granite", "src": "zucchi",
         "scraped": "Via Lactea", "sources": ["polonine", "zucchi"]}])
    card = decisions_store.list_pending("variety")[0]
    assert card["listings"] == [{"source": "polonine", "scraped": "VIA LACTEA"},
                                {"source": "zucchi", "scraped": "Via Lactea"}]
    assert card["spellings"] == ["VIA LACTEA", "Via Lactea"]
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"listings": card["listings"], "name": "Golden Lightning", "type": "Granite"})
    assert code == 200 and body["decided"] == 2
    assert {k for k in views.scoped(exists_as=_exists)} == {("polonine", _norm("VIA LACTEA")), ("zucchi", _norm("Via Lactea"))}
    code, body = server.dispatch("DELETE", ["review", "decide"], {"listings": card["listings"]})
    assert code == 200 and len(body["cleared"]) == 2 and views.scoped(exists_as=_exists) == {}


def test_decide_without_any_listing_is_400():
    code, _ = server.dispatch("PUT", ["review", "decide"], {"name": "X", "type": "Granite"})
    assert code == 400
    code, _ = server.dispatch("PUT", ["review", "decide"], {"scraped": "X", "name": "X", "type": "Granite"})
    assert code == 400                                                     # a spelling without its vendor


def test_a_renamed_mint_documents_its_origin_under_the_new_name():
    # the origin map is looked up by the variety's NAME: a mint + rename with a country must overlay the
    # country under the name the variety is created with, not under the scraped spelling
    decisions_store.decide("varsha", "Arctic White", "Arctic White Varsha", "Quartzite", origin="IN", exists_as=_exists)
    assert views.country_rules(exists_as=_exists) == {("arctic white varsha", "quartzite"): "IN"}
    assert views.countries(exists_as=_exists) == {"arctic white varsha": "IN"}
    decisions_store.decide("zucchi", "Acquaclara", "Acquaclara", "Quartzite", origin="BR", exists_as=_exists)
    assert views.country_rules(exists_as=_exists)[("acquaclara", "quartzite")] == "BR"


# --- two levels: a spelling means one variety for everyone; a vendor may decide otherwise for itself --------

def test_first_mint_on_a_spelling_is_global_and_a_same_stone_restatement_changes_nothing():
    out = decisions_store.decide("polonine", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    g = views.actions(exists_as=_exists)[("", _norm("Tropical Green"))]
    assert g["source"] == "" and g["asked_by"] == "polonine" and g["seed_name"] == "Tropical Green Bahia"
    assert views.seed_scopes(exists_as=_exists) == {}
    assert views.scoped(exists_as=_exists) == {}                        # the rename attaches for everyone (curate)
    # zucchi states the same stone: nothing new is stored, its product binds through the global meaning
    out = decisions_store.decide("zucchi", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    assert views.seed_scopes(exists_as=_exists) == {} and views.scoped(exists_as=_exists) == {}


def test_a_different_new_stone_from_the_same_spelling_is_the_vendors_own_level():
    decisions_store.decide("polonine", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    out = decisions_store.decide("zucchi", "Tropical Green", "Tropical Green Verde", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "vendor"
    assert views.actions(exists_as=_exists)[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"   # global kept
    v = views.actions(exists_as=_exists)[("zucchi", _norm("Tropical Green"))]
    assert v["seed_name"] == "Tropical Green Verde" and v["source"] == "zucchi"
    assert views.scoped(exists_as=_exists)[("zucchi", _norm("Tropical Green"))] == ("Tropical Green Verde", "Granite")
    assert views.seed_scopes(exists_as=_exists) == {("zucchi", _norm("Tropical Green")): "zucchi"}
    # both levels reach the readers: the merged map carries the global under the spelling, the vendor's under its key
    both = views.actions(exists_as=_exists)
    assert both[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"
    assert both[("zucchi", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Verde"
    # clearing zucchi's statement removes only zucchi's level
    assert {"statements": decisions_store.clear("zucchi", "Tropical Green")}["statements"] == 1
    assert views.seed_scopes(exists_as=_exists) == {}
    assert views.actions(exists_as=_exists)[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"


def test_a_spelling_that_already_means_an_existing_stone_makes_the_mint_vendor_level():
    # every vendor's 'Brown Granite' binds to the existing Brown Granite; marenostone says ITS one is a new stone
    out = decisions_store.decide("marenostone", "Brown Granite", "Chocolate Classic", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "vendor"
    assert ("", _norm("Brown Granite")) not in views.actions(exists_as=_exists)   # the global meaning stays Brown Granite
    v = views.actions(exists_as=_exists)[("marenostone", _norm("Brown Granite"))]
    assert v["seed_name"] == "Chocolate Classic" and v["source"] == "marenostone"
    assert views.scoped(exists_as=_exists)[("marenostone", _norm("Brown Granite"))] == ("Chocolate Classic", "Granite")
    # the same through a known alias of an existing stone, on the CLEANED form of the spelling
    alias = lambda n, t: "Silver Waves" if (_norm(n), _norm(t)) == ("black wave", "marble") else None
    out = decisions_store.decide("marenostone", "Black Wave Marble", "Mercury Black", "Marble",
                                 exists_as=_exists, alias_target=alias)
    assert out["level"] == "vendor"
    assert ("marenostone", _norm("Black Wave Marble")) in views.actions(exists_as=_exists)


# --- audit wave 1: a statement supersedes a reject on the same listing -------------------------------------

def test_a_statement_supersedes_a_reject_stored_under_the_card_ref():
    # The reject PUT keys on the card ref (the CLEANED name, 'amazon green'); a statement keys on the scraped
    # spelling ('Amazon Green Granite'). Both rows coexisted and curate consulted the reject first, so the
    # statement never applied and only the audit gap revealed it. The last operator action must win.
    decisions_store.reject("", "amazon green")                    # what the reject PUT stores
    out = decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite",
                                 exists_as=_exists)
    assert out["result"] == "bound"
    assert views.rejected(exists_as=_exists) == set()                                  # the reject is gone
    assert views.confirm(exists_as=_exists) == {}
    # and a reject stored under the SPELLING itself is superseded the same way, for a mint
    decisions_store.reject("", "Totally New")
    out = decisions_store.decide("zucchi", "Totally New", "Totally New", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and views.rejected(exists_as=_exists) == set()
    assert views.confirm(exists_as=_exists) == {("", _norm("Totally New")): "yes"}


def test_vendor_rename_to_the_same_name_wins_the_origin_regardless_of_insert_order():
    # two decisions can land on one variety NAME: a global mint renamed to "Bar" (IN) and a vendor rename to
    # "Bar" (BR). The name-keyed origin maps must resolve that collision deterministically (the vendor's own
    # level wins), not by SQLite rowid / insert order.
    views.mint("Foo", stone_type="Granite", name="Bar", origin="BR", source="zucchi", asked_by="zucchi")
    views.mint("Baz", stone_type="Granite", name="Bar", origin="IN", source="", asked_by="polonine")
    assert views.country_rules(exists_as=_exists)[("bar", "granite")] == "BR"
