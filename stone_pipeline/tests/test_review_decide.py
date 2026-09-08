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

EXISTING = {("golden lightning", "granite"), ("azul white", "onyx"), ("azul white", "quartzite")}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "config_db_path", lambda: tmp_path / "config.db")
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: (_norm(n), _norm(t)) in EXISTING)
    monkeypatch.setattr(server, "_type_vocab", lambda: {"granite": "Granite", "onyx": "Onyx", "quartzite": "Quartzite"})
    yield


def _exists(n, t):
    return (_norm(n), _norm(t)) in EXISTING


def test_existing_variety_binds_for_the_vendor_with_its_origin():
    out = decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite",
                                 origin="IR", exists_as=_exists)
    assert out["result"] == "bound"
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Amazon Green Granite"))] == ("Golden Lightning", "Granite")
    assert decisions_store.origin_decisions()[("marenostone", "golden lightning", "granite")] == "IR"
    assert decisions_store.variety_actions() == {}                       # nothing minted
    assert decisions_store.variety_origins() == {}                       # documented origins untouched


def test_unknown_variety_mints_globally_and_binds_the_vendor_spelling():
    out = decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White Persian", "Onyx",
                                 color="White", origin="IR", exists_as=_exists)
    assert out["result"] == "minted"
    dec = decisions_store.variety_actions()[_norm("Azul White Quartzite")]
    assert dec["action"] == "mint" and dec["seed_name"] == "Azul White Persian"
    assert dec["seed_type"] == "Onyx" and dec["seed_color"] == "White" and dec["seed_country"] == "IR"
    assert dec["source"] == "marenostone"
    # the rename binds THIS vendor's spelling through a scoped alias, never a global one
    assert decisions_store.variety_seed_scopes() == {_norm("Azul White Quartzite"): "marenostone"}
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Azul White Quartzite"))] == ("Azul White Persian", "Onyx")
    assert decisions_store.origin_decisions()[("marenostone", "azul white persian", "onyx")] == "IR"


def test_same_name_mint_is_not_a_rename():
    decisions_store.decide("varsha", "Bonsai", "Bonsai", "Quartzite", exists_as=_exists)
    dec = decisions_store.variety_actions()[_norm("Bonsai")]
    assert dec["action"] == "mint" and dec["seed_name"] is None
    assert decisions_store.scoped_aliases() == {}
    assert decisions_store.variety_seed_scopes() == {}


def test_widen_adds_the_origin_to_the_variety_for_every_vendor():
    decisions_store.decide("marenostone", "Golden Lightning", "Golden Lightning", "Granite",
                           origin="IR", widen=True, exists_as=_exists)
    assert decisions_store.variety_origins()[("golden lightning", "granite")] == "IR"
    assert decisions_store.origin_decisions()[("marenostone", "golden lightning", "granite")] == "IR"


def test_redeciding_overwrites_and_clearing_removes_exactly_that_vendors_decisions():
    decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White", "Quartzite", origin="BR",
                           exists_as=_exists)
    decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White", "Onyx", origin="IR",
                           exists_as=_exists)
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Azul White Quartzite"))] == ("Azul White", "Onyx")
    # another vendor's decision on the same spelling is independent
    decisions_store.decide("zucchi", "Azul White Quartzite", "Azul White", "Quartzite", origin="BR",
                           exists_as=_exists)
    dropped = decisions_store.clear_decisions("marenostone", "Azul White Quartzite")
    assert dropped == {"scoped_alias": 1, "mint": 0, "origin": 1}
    assert ("marenostone", _norm("Azul White Quartzite")) not in decisions_store.scoped_aliases()
    assert decisions_store.scoped_aliases()[("zucchi", _norm("Azul White Quartzite"))] == ("Azul White", "Quartzite")
    assert decisions_store.origin_decisions() == {("zucchi", "azul white", "quartzite"): "BR"}


def test_clear_leaves_a_global_mint_alone():
    decisions_store.set_variety_decision("Totally New", "mint", seed_type="Granite")      # made for every vendor
    dropped = decisions_store.clear_decisions("marenostone", "Totally New")
    assert dropped["mint"] == 0
    assert decisions_store.variety_actions()[_norm("Totally New")]["action"] == "mint"


def test_missing_fields_store_nothing():
    with pytest.raises(decisions_store.InvalidDecision):
        decisions_store.decide("marenostone", "Azul White", "", "Onyx", exists_as=_exists)
    assert decisions_store.scoped_aliases() == {} and decisions_store.variety_actions() == {}


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
    assert _norm("X") not in decisions_store.variety_actions()


def test_put_decide_applies_one_statement_to_every_spelling_on_a_card():
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": ["Amazon Green Granite", "Amazon Green Granite Slab"],
                                  "name": "Golden Lightning", "type": "Granite", "origin": "IR"})
    assert code == 200 and body["decided"] == 2
    keys = {k for k in decisions_store.scoped_aliases() if k[0] == "marenostone"}
    assert keys == {("marenostone", _norm("Amazon Green Granite")), ("marenostone", _norm("Amazon Green Granite Slab"))}


def test_put_decide_refuses_a_retired_variety(monkeypatch):
    from stone_pipeline.stages import decisions as stage_decisions
    from stone_pipeline.stages.curate import active_branches, gen_key
    key = gen_key(active_branches()[0], "Onyx", "Old Stone")
    monkeypatch.setattr(stage_decisions, "load_retired", lambda: {key})
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Old Stone", "name": "Old Stone", "type": "Onyx"})
    assert code == 409 and body["retired"]
    assert decisions_store.variety_actions() == {}


def test_delete_decide_clears():
    server.dispatch("PUT", ["review", "decide"], {"source": "marenostone", "scraped": "Amazon Green Granite",
                                                  "name": "Golden Lightning", "type": "Granite", "origin": "IR"})
    code, body = server.dispatch("DELETE", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Amazon Green Granite"})
    assert code == 200 and body["cleared"] == [{"scoped_alias": 1, "mint": 0, "origin": 1}]
    assert decisions_store.scoped_aliases() == {}


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

def test_a_vendor_restating_a_global_mint_keeps_it_global():
    decisions_store.set_variety_decision("Totally New", "mint", seed_type="Granite")          # every vendor
    decisions_store.decide("marenostone", "Totally New", "Totally New Stone", "Granite", exists_as=_exists)
    dec = decisions_store.variety_actions()[_norm("Totally New")]
    assert dec["source"] == "" and dec["seed_name"] == "Totally New Stone"
    assert decisions_store.variety_seed_scopes() == {}                     # the global alias attach still runs
    assert decisions_store.scoped_aliases() == {}


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
    assert {k for k in decisions_store.scoped_aliases()} == {("polonine", _norm("VIA LACTEA")), ("zucchi", _norm("Via Lactea"))}
    code, body = server.dispatch("DELETE", ["review", "decide"], {"listings": card["listings"]})
    assert code == 200 and len(body["cleared"]) == 2 and decisions_store.scoped_aliases() == {}


def test_decide_without_any_listing_is_400():
    code, _ = server.dispatch("PUT", ["review", "decide"], {"name": "X", "type": "Granite"})
    assert code == 400
    code, _ = server.dispatch("PUT", ["review", "decide"], {"scraped": "X", "name": "X", "type": "Granite"})
    assert code == 400                                                     # a spelling without its vendor


def test_a_renamed_mint_documents_its_origin_under_the_new_name():
    # the origin map is looked up by the variety's NAME: a mint + rename with a country must overlay the
    # country under the name the variety is created with, not under the scraped spelling
    decisions_store.decide("varsha", "Arctic White", "Arctic White Varsha", "Quartzite", origin="IN", exists_as=_exists)
    assert decisions_store.variety_seed_country_rules() == {("arctic white varsha", "quartzite"): "IN"}
    assert decisions_store.variety_seed_countries() == {"arctic white varsha": "IN"}
    decisions_store.decide("zucchi", "Acquaclara", "Acquaclara", "Quartzite", origin="BR", exists_as=_exists)
    assert decisions_store.variety_seed_country_rules()[("acquaclara", "quartzite")] == "BR"
