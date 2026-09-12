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
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Amazon Green Granite"))] == ("Golden Lightning", "Granite")
    assert decisions_store.origin_decisions()[("marenostone", "golden lightning", "granite")] == "IR"
    assert decisions_store.variety_actions() == {}                       # nothing minted
    assert decisions_store.variety_origins() == {}                       # documented origins untouched


def test_unknown_variety_mints_globally_and_binds_the_vendor_spelling():
    out = decisions_store.decide("marenostone", "Azul White Quartzite", "Azul White Persian", "Onyx",
                                 color="White", origin="IR", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    dec = decisions_store.variety_actions()[("", _norm("Azul White Quartzite"))]
    assert dec["action"] == "mint" and dec["seed_name"] == "Azul White Persian"
    assert dec["seed_type"] == "Onyx" and dec["seed_color"] == "White" and dec["seed_country"] == "IR"
    assert dec["source"] == "" and dec["asked_by"] == "marenostone"          # the first mint is the spelling's meaning
    assert decisions_store.variety_seed_scopes() == {} and decisions_store.scoped_aliases() == {}
    assert decisions_store.origin_decisions()[("marenostone", "azul white persian", "onyx")] == "IR"


def test_name_that_is_an_existing_alias_binds_to_its_variety_not_a_mint(monkeypatch):
    # an origin card names 'Artemis' (already an alias of 'Andes' Quartzite): the statement must bind the
    # vendor's spelling to Andes and record Andes' origin, never mint a duplicate variety 'Artemis'
    monkeypatch.setattr(varieties, "alias_target",
                        lambda n, t: "Andes" if (_norm(n), _norm(t)) == ("artemis", "quartzite") else None)
    out = decisions_store.decide("polonine", "ARTEMIS", "Artemis", "Quartzite", origin="BR", widen=True,
                                 exists_as=_exists)
    assert out["result"] == "bound" and out["resolved_alias"] is True and out["name"] == "Andes"
    assert decisions_store.scoped_aliases()[("polonine", _norm("ARTEMIS"))] == ("Andes", "Quartzite")
    assert decisions_store.origin_decisions()[("polonine", "andes", "quartzite")] == "BR"
    assert decisions_store.variety_actions() == {}                       # nothing minted -- no duplicate Artemis
    assert decisions_store.origin_widen()[("polonine", "andes", "quartzite")] == "BR"   # widen keyed to the target
    assert decisions_store.variety_origins() == {}                                        # the list is unioned at load, never rewritten


def test_same_name_mint_is_not_a_rename():
    decisions_store.decide("varsha", "Bonsai", "Bonsai", "Quartzite", exists_as=_exists)
    dec = decisions_store.variety_actions()[("", _norm("Bonsai"))]
    assert dec["action"] == "mint" and dec["seed_name"] is None
    assert decisions_store.scoped_aliases() == {}
    assert decisions_store.variety_seed_scopes() == {}


def test_widen_is_recorded_on_the_decision_and_never_rewrites_the_stones_list():
    from stone_pipeline.reference.loaders import OriginMap, OriginRule
    decisions_store.decide("marenostone", "Golden Lightning", "Golden Lightning", "Granite",
                           origin="IR", widen=True, exists_as=_exists)
    assert decisions_store.origin_decisions()[("marenostone", "golden lightning", "granite")] == "IR"
    assert decisions_store.origin_widen() == {("marenostone", "golden lightning", "granite"): "IR"}
    assert decisions_store.variety_origins() == {}                     # the explicit list edit is untouched
    # at load the widened country is ADDED to what the map documents, never replacing it
    m = OriginMap(rules=[OriginRule(variety="Golden Lightning", country_iso="BR,CN", city="", county="", stone_type="Granite")])
    assert m.widen(decisions_store.origin_widen()) == 1
    assert m.exact("Golden Lightning", "Granite").countries == ["BR", "CN", "IR"]
    assert m.widen(decisions_store.origin_widen()) == 0                 # idempotent
    # a stone the map does not document yet gets its first documented origin
    decisions_store.decide("zucchi", "Andes", "Andes", "Quartzite", origin="BR", widen=True, exists_as=_exists)
    assert m.widen(decisions_store.origin_widen()) == 1 and m.exact("Andes", "Quartzite").countries == ["BR"]


def test_store_repairs_the_lists_an_old_widen_rewrote():
    # before the union: widen WROTE the variety's list as its one country. Such a row (single country equal to
    # a widen decision on the same variety) is dropped on open; an explicit list edit (any other row) stays.
    decisions_store.set_origin_decision("varsha", "Black Cosmic", "Granite", "IN", widen=True)
    decisions_store.set_variety_origin("Black Cosmic", "Granite", "IN")            # what the old widen wrote
    decisions_store.set_variety_origin("Volakas", "Marble", "GR,TR")                  # an operator list edit
    decisions_store.set_variety_origin("Porto Branco", "Granite", "PT")               # an edit, no widen on it
    import sqlite3
    with sqlite3.connect(str(store.config_db_path())) as raw:
        raw.execute("PRAGMA user_version = 1")               # a database from before the repair: migrates on open
    conn = store.open_store(); conn.close()
    assert decisions_store.variety_origins() == {("volakas", "marble"): "GR,TR", ("porto branco", "granite"): "PT"}


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
    assert decisions_store.variety_actions()[("", _norm("Totally New"))]["action"] == "mint"


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
    assert ("", _norm("X")) not in decisions_store.variety_actions()


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

def test_a_vendor_naming_a_different_stone_from_a_global_mint_gets_its_own_level():
    decisions_store.set_variety_decision("Totally New", "mint", seed_type="Granite")          # every vendor
    out = decisions_store.decide("marenostone", "Totally New", "Totally New Stone", "Granite", exists_as=_exists)
    assert out["level"] == "vendor"
    assert decisions_store.variety_actions()[("", _norm("Totally New"))]["seed_name"] is None     # the global is untouched
    assert decisions_store.variety_actions()[("marenostone", _norm("Totally New"))]["seed_name"] == "Totally New Stone"
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Totally New"))] == ("Totally New Stone", "Granite")


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


# --- two levels: a spelling means one variety for everyone; a vendor may decide otherwise for itself --------

def test_first_mint_on_a_spelling_is_global_and_a_same_stone_restatement_changes_nothing():
    out = decisions_store.decide("polonine", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    g = decisions_store.variety_actions()[("", _norm("Tropical Green"))]
    assert g["source"] == "" and g["asked_by"] == "polonine" and g["seed_name"] == "Tropical Green Bahia"
    assert decisions_store.variety_seed_scopes() == {}
    assert decisions_store.scoped_aliases() == {}                        # the rename attaches for everyone (curate)
    # zucchi states the same stone: nothing new is stored, its product binds through the global meaning
    out = decisions_store.decide("zucchi", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "global"
    assert decisions_store.variety_seed_scopes() == {} and decisions_store.scoped_aliases() == {}


def test_a_different_new_stone_from_the_same_spelling_is_the_vendors_own_level():
    decisions_store.decide("polonine", "Tropical Green", "Tropical Green Bahia", "Granite", exists_as=_exists)
    out = decisions_store.decide("zucchi", "Tropical Green", "Tropical Green Verde", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "vendor"
    assert decisions_store.variety_actions()[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"   # global kept
    v = decisions_store.variety_actions()[("zucchi", _norm("Tropical Green"))]
    assert v["seed_name"] == "Tropical Green Verde" and v["source"] == "zucchi"
    assert decisions_store.scoped_aliases()[("zucchi", _norm("Tropical Green"))] == ("Tropical Green Verde", "Granite")
    assert decisions_store.variety_seed_scopes() == {("zucchi", _norm("Tropical Green")): "zucchi"}
    # both levels reach the readers: the merged map carries the global under the spelling, the vendor's under its key
    both = decisions_store.variety_actions()
    assert both[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"
    assert both[("zucchi", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Verde"
    # clearing zucchi's statement removes only zucchi's level
    assert decisions_store.clear_decisions("zucchi", "Tropical Green")["mint"] == 1
    assert decisions_store.variety_seed_scopes() == {}
    assert decisions_store.variety_actions()[("", _norm("Tropical Green"))]["seed_name"] == "Tropical Green Bahia"


def test_a_spelling_that_already_means_an_existing_stone_makes_the_mint_vendor_level():
    # every vendor's 'Brown Granite' binds to the existing Brown Granite; marenostone says ITS one is a new stone
    out = decisions_store.decide("marenostone", "Brown Granite", "Chocolate Classic", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and out["level"] == "vendor"
    assert ("", _norm("Brown Granite")) not in decisions_store.variety_actions()   # the global meaning stays Brown Granite
    v = decisions_store.variety_actions()[("marenostone", _norm("Brown Granite"))]
    assert v["seed_name"] == "Chocolate Classic" and v["source"] == "marenostone"
    assert decisions_store.scoped_aliases()[("marenostone", _norm("Brown Granite"))] == ("Chocolate Classic", "Granite")
    # the same through a known alias of an existing stone, on the CLEANED form of the spelling
    alias = lambda n, t: "Silver Waves" if (_norm(n), _norm(t)) == ("black wave", "marble") else None
    out = decisions_store.decide("marenostone", "Black Wave Marble", "Mercury Black", "Marble",
                                 exists_as=_exists, alias_target=alias)
    assert out["level"] == "vendor"
    assert ("marenostone", _norm("Black Wave Marble")) in decisions_store.variety_actions()


def test_backfill_moves_a_global_mint_whose_spelling_means_another_stone_to_the_vendor(tmp_path, monkeypatch):
    # the migrated state: every old mint global, asked_by = the vendor that stated it
    decisions_store.set_variety_decision("Brown Granite", "mint", seed_type="Granite", seed_name="Chocolate Classic",
                                         source="", asked_by="marenostone")
    decisions_store.set_variety_decision("Bianco White Marble", "mint", seed_type="Marble", seed_name="Bianco White",
                                         source="", asked_by="marenostone")
    decisions_store.set_variety_decision("Honey Onyx", "mint", seed_type="Onyx", seed_name="Honey",
                                         source="", asked_by="zucchi")
    # ... after Honey was minted its spelling is Honey's own alias: the same stone, never a move
    alias = lambda n, t: "Honey" if (_norm(n), _norm(t)) == ("honey onyx", "onyx") else None
    moved = decisions_store.backfill_levels(exists_as=_exists, alias_target=alias)
    assert [(m["source"], m["scraped"], m["name"], m["spelling_means"]) for m in moved] == [
        ("marenostone", "Brown Granite", "Chocolate Classic", "Brown Granite")]
    assert set(decisions_store.variety_actions()) == {("", _norm("Bianco White Marble")), ("", _norm("Honey Onyx")),
                                                      ("marenostone", _norm("Brown Granite"))}
    assert decisions_store.variety_actions()[("marenostone", _norm("Brown Granite"))]["seed_name"] == "Chocolate Classic"
    assert decisions_store.scoped_aliases() == {("marenostone", _norm("Brown Granite")): ("Chocolate Classic", "Granite")}
    assert decisions_store.backfill_levels(exists_as=_exists, alias_target=alias) is None    # applied once, never again
    decisions_store.set_variety_decision("Chocolate Granite", "mint", seed_type="Granite", seed_name="Chocolate Classic",
                                         source="", asked_by="marenostone")
    assert decisions_store.backfill_levels(exists_as=lambda n, t: True, alias_target=alias) is None
    assert ("marenostone", _norm("Chocolate Granite")) not in decisions_store.variety_actions()


def test_migration_makes_every_old_mint_global_and_keeps_who_asked(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    monkeypatch.setattr(store, "config_db_path", lambda: db)
    store.open_store().close()                                           # the real schema, then the OLD mint table
    conn = sqlite3.connect(db)
    conn.executescript(
        "DROP TABLE variety_decision;"
        "CREATE TABLE variety_decision (variant_norm TEXT PRIMARY KEY, variant_display TEXT NOT NULL DEFAULT '', "
        "action TEXT NOT NULL CHECK (action IN ('mint','reject','alias')), alias_of TEXT, seed_color TEXT, "
        "seed_type TEXT, seed_country TEXT, seed_name TEXT, decided_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT '');"
        "INSERT INTO variety_decision VALUES ('bianco white marble','Bianco White Marble','mint',NULL,'White','Marble','IR','Bianco White','2026-09-09T07:20:56','marenostone');"
        "INSERT INTO variety_decision VALUES ('junk','Junk','reject',NULL,NULL,NULL,NULL,NULL,'2026-09-09T08:00:00','');")
    conn.execute("PRAGMA user_version = 1")                    # pre two-levels: migrates on open
    conn.commit(); conn.close()
    g = decisions_store.variety_actions()
    assert g[("", _norm("bianco white marble"))] == {"action": "mint", "alias_of": None, "seed_color": "White", "seed_type": "Marble",
                                              "seed_country": "IR", "seed_name": "Bianco White", "spelling": "Bianco White Marble",
                                              "source": "", "asked_by": "marenostone"}
    assert g[("", "junk")]["action"] == "reject" and g[("", "junk")]["asked_by"] == ""
    assert decisions_store.variety_seed_scopes() == {}
    conn = sqlite3.connect(db)
    assert conn.execute("select count(*) from variety_decision__pre_two_levels").fetchone()[0] == 2
    assert [r[1] for r in conn.execute("pragma table_info(variety_decision)") if r[5]] == ["source", "variant_norm"]
    conn.close()
    decisions_store.variety_actions()                                    # a second open: idempotent, nothing changes
    assert len(decisions_store.variety_actions()) == 2


# --- audit wave 1: a statement supersedes a reject on the same listing -------------------------------------

def test_a_statement_supersedes_a_reject_stored_under_the_card_ref():
    # The reject PUT keys on the card ref (the CLEANED name, 'amazon green'); a statement keys on the scraped
    # spelling ('Amazon Green Granite'). Both rows coexisted and curate consulted the reject first, so the
    # statement never applied and only the audit gap revealed it. The last operator action must win.
    decisions_store.set_variety_decision("amazon green", "reject")                    # what the reject PUT stores
    out = decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite",
                                 exists_as=_exists)
    assert out["result"] == "bound"
    assert decisions_store.rejected_names() == set()                                  # the reject is gone
    assert decisions_store.confirm_map() == {}
    # and a reject stored under the SPELLING itself is superseded the same way, for a mint
    decisions_store.set_variety_decision("Totally New", "reject")
    out = decisions_store.decide("zucchi", "Totally New", "Totally New", "Granite", exists_as=_exists)
    assert out["result"] == "minted" and decisions_store.rejected_names() == set()
    assert decisions_store.confirm_map() == {("", _norm("Totally New")): "yes"}


def test_vendor_rename_to_the_same_name_wins_the_origin_regardless_of_insert_order():
    # two decisions can land on one variety NAME: a global mint renamed to "Bar" (IN) and a vendor rename to
    # "Bar" (BR). The name-keyed origin maps must resolve that collision deterministically (the vendor's own
    # level wins), not by SQLite rowid / insert order.
    decisions_store.set_variety_decision("Foo", "mint", seed_type="Granite", seed_name="Bar", seed_country="BR",
                                         source="zucchi", asked_by="zucchi")
    decisions_store.set_variety_decision("Baz", "mint", seed_type="Granite", seed_name="Bar", seed_country="IN",
                                         source="", asked_by="polonine")
    assert decisions_store.variety_seed_country_rules()[("bar", "granite")] == "BR"
