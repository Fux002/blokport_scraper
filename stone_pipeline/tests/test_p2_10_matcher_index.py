"""P2-10 C3: the matcher's index from the alias stores (P2-10-SCRAPER-PLAN.md section 3; P2-CHUNKS P2-10 row).
After P2-7 the backend's variants export carries the hand rows only, as AdminAliases and AdminCodes (|-joined, table
230 E1), and no Aliases column; the scraper reads them by header as match surfaces. A code (the brand's
name_code_pattern) is a surface of the EXACT tier only: never a projection, phonetic or fuzzy surface, so 'G630' never
resolves to the variety of 'G603'. No em dashes."""
from __future__ import annotations

from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.reference.medusa_files import read_variants_export

_P2_7_EXPORT = (
    "Id,Key,Name,Image,AdminAliases,AdminCodes,status,Type,owner\n"
    "var_1,granite__black_galaxy_1,Black Galaxy,,Star Galaxy|Galaxy Black,G603,active,Granite,\n"
    "var_2,granite__padang_dark_2,Padang Dark,,,,active,Granite,\n")


def test_the_export_reader_reads_the_hand_aliases_and_codes_by_header():
    rows = read_variants_export(_P2_7_EXPORT, require_status=True)
    assert [(r.key, r.admin_aliases, r.admin_codes, r.aliases) for r in rows] == [
        ("granite__black_galaxy_1", ("Star Galaxy", "Galaxy Black"), ("G603",), ()),
        ("granite__padang_dark_2", (), (), ())]


def test_a_base_without_the_admin_columns_reads_them_empty():
    rows = read_variants_export("Key,Name,Type,Aliases\ngranite__x_1,X,Granite,Ex\n", require_status=False)
    assert [(r.aliases, r.admin_aliases, r.admin_codes) for r in rows] == [(("Ex",), (), ())]


def _engine() -> tuple[CandidateIndex, VariationEngine]:
    idx = CandidateIndex()
    idx.add("g1", "Black Galaxy", surfaces=[], block_type="granite")
    idx.add_code("g1", "G603")
    idx.add("g2", "Padang Dark", surfaces=[], block_type="granite")
    idx.add_code("g2", "G654")
    return idx, VariationEngine(idx, auto_accept=92, review_floor=84)


def test_a_code_resolves_in_the_exact_tier():
    _, eng = _engine()
    m = eng.match("g603")
    assert (m.cid, m.method) == ("g1", "exact")


def test_a_code_is_no_surface_of_the_similarity_tiers():
    idx, eng = _engine()
    assert "G603" not in idx.candidates["g1"].surfaces          # fuzzy and overlap score against surfaces
    assert idx.lookup_phonetic("G603") == set() and idx.lookup_compact("G603") == set()
    assert eng.match("G630").cid is None                        # one digit off is another stone, never a guess


# --- the surfaces builder: every store, one code boundary, one precedence -------------------------------------------
from stone_pipeline.aliases.surfaces import OperatorAlias, Stores, Surfaces, Variety, surfaces_by_key  # noqa: E402
from stone_pipeline.core.alias_problem import code_matcher  # noqa: E402

_IS_CODE = code_matcher(r"[Gg]\d+[A-Za-z]?")       # the stone pack's name_code_pattern
_GALAXY, _PADANG, _COFFEE_M, _COFFEE_O = (Variety("granite__black_galaxy_1", "Black Galaxy", "Granite"),
                                          Variety("granite__padang_dark_2", "Padang Dark", "Granite"),
                                          Variety("marble__coffee_3", "Coffee", "Marble"),
                                          Variety("onyx__coffee_4", "Coffee", "Onyx"))


def test_each_store_reaches_the_variety_with_its_method():
    stores = Stores(seed={_GALAXY.key: (["Star Galaxy"], [])},
                    operator=[OperatorAlias("Black Galaxy", "Granite", "Galaxy Nero")],
                    learned=[(_GALAXY.key, "Blak Galaxy")],
                    admin={_GALAXY.key: (["Galaxy Black"], [])})
    got, unbound = surfaces_by_key([_GALAXY], stores, _IS_CODE)
    assert got[_GALAXY.key].aliases == (("Galaxy Nero", "operator"), ("Star Galaxy", "seed"),
                                        ("Blak Galaxy", "learned"), ("Galaxy Black", "admin"))
    assert unbound == []


def test_a_code_shaped_value_is_a_code_in_every_store():
    stores = Stores(seed={_GALAXY.key: (["G603"], [])},
                    operator=[OperatorAlias("Padang Dark", "Granite", "g654")],
                    learned=[(_GALAXY.key, "G603A")],
                    admin={_PADANG.key: (["G654 G684"], [])})
    got, _ = surfaces_by_key([_GALAXY, _PADANG], stores, _IS_CODE)
    assert got[_GALAXY.key] == Surfaces(aliases=(), codes=(("G603", "seed"), ("G603A", "learned")))
    assert got[_PADANG.key] == Surfaces(aliases=(), codes=(("g654", "operator"), ("G654 G684", "admin")))


def test_a_value_in_two_stores_sits_once_under_the_first_in_precedence():
    # operator over seed over learned, the hand rows last (case and accents are one value: match_key)
    stores = Stores(seed={_GALAXY.key: (["Star Galaxy"], [])},
                    operator=[OperatorAlias("Black Galaxy", "Granite", "STAR GALAXY")],
                    learned=[(_GALAXY.key, "star galaxy")], admin={_GALAXY.key: (["Star Galaxy"], [])})
    got, _ = surfaces_by_key([_GALAXY], stores, _IS_CODE)
    assert got[_GALAXY.key].aliases == (("STAR GALAXY", "operator"),)


def test_an_operator_alias_naming_no_one_variety_is_listed_never_guessed():
    untyped = OperatorAlias("Coffee", "", "Cafe")            # Coffee is a marble AND an onyx
    typed = OperatorAlias("Coffee", "Onyx", "Cafe Onyx")
    nowhere = OperatorAlias("Unknown Stone", "Marble", "Unk")
    got, unbound = surfaces_by_key([_COFFEE_M, _COFFEE_O], Stores(operator=[untyped, typed, nowhere]), _IS_CODE)
    assert got[_COFFEE_O.key].aliases == (("Cafe Onyx", "operator"),) and got[_COFFEE_M.key].aliases == ()
    assert unbound == [untyped, nowhere]


# --- the variety table: its surfaces come from the stores, never from the file's Aliases column ---------------------
def test_the_variety_table_takes_its_surfaces_from_the_stores(tmp_path):
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.reference.loaders import load_variants
    galaxy = new_key("Granite", "Black Galaxy")
    path = tmp_path / "variants_export.csv"
    path.write_text("Key,Name,Type,Aliases,AdminAliases,AdminCodes\n"
                    f"{galaxy},Black Galaxy,Granite,Base Only,Galaxy Black,G654\n", encoding="utf-8")
    table = load_variants(path, Stores(seed={galaxy: (["Star Galaxy", "G603"], [])}))
    variant = table.by_id[galaxy]
    assert (variant.aliases, variant.codes) == (["Star Galaxy", "Galaxy Black"], ["G603", "G654"])


def test_the_index_puts_a_variety_s_codes_in_the_exact_tier_only():
    from stone_pipeline.matching.index import build_variation_index
    from stone_pipeline.reference.loaders.backbone import Backbone
    from stone_pipeline.reference.loaders.variants import Variant, VariantTable
    from stone_pipeline.phase1.variety_key import new_key
    galaxy = new_key("Granite", "Black Galaxy")
    table = VariantTable()
    table.by_id["v1"] = Variant(variation_id="v1", key=galaxy, name="Black Galaxy", aliases=["Star Galaxy"],
                                codes=["G603"])
    index = build_variation_index(table, Backbone())
    assert index.lookup_norm("G603") == {"v1"} and index.lookup_norm("Star Galaxy") == {"v1"}
    assert "G603" not in index.candidates["v1"].surfaces and index.lookup_phonetic("G603") == set()


# --- alias_target and the matcher read the same surfaces ------------------------------------------------------------
def test_alias_target_and_the_matcher_agree_on_every_store(tmp_path, monkeypatch):
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.config import varieties
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    from stone_pipeline.matching.index import build_variation_index
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.reference.loaders import load_variants
    from stone_pipeline.reference.loaders.backbone import Backbone
    casa, galaxy = new_key("Marble", "Casa Blue"), new_key("Granite", "Black Galaxy")
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Image,AdminAliases,AdminCodes,status,Type,owner\n"
                      f"var_c,{casa},Casa Blue,,,,active,Marble,\n"
                      f"var_g,{galaxy},Black Galaxy,,Star Galaxy,G603,active,Granite,\n", encoding="utf-8")
    ledger = tmp_path / "dev.ledger"
    with Ledger.open(ledger, env=writethrough.ENV_NAME) as lg:
        for key, name, typ in ((casa, "Casa Blue", "Marble"), (galaxy, "Black Galaxy", "Granite")):
            lg.upsert("variation", {"key": key, "type": typ, "name": name, "aliases": "[]", "medusa_id": None,
                                    "payload_hash": "", "state": "pending", "first_seen": "t", "last_synced": None,
                                    "created_at": "t", "updated_at": "t"}, pk=("key",))
    scraper = Stores(seed={casa: (["Azul Casa"], [])},
                     operator=[OperatorAlias("Black Galaxy", "Granite", "Galaxy Nero")],
                     learned=[(casa, "Casablue")])
    monkeypatch.setattr(alias_stores, "scraper_stores", lambda backbone=None: scraper)
    monkeypatch.setattr(writethrough, "ledger_path", lambda: ledger)
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: set())
    monkeypatch.setattr(alias_stores, "export_file", lambda: export)
    monkeypatch.setitem(varieties._CACHE, "fp", None)            # the ledger index is rebuilt on these stores
    engine = VariationEngine(build_variation_index(load_variants(export, scraper), Backbone()), 92, 84)
    for value, typ, name in (("Azul Casa", "Marble", "Casa Blue"), ("Casablue", "Marble", "Casa Blue"),
                             ("Galaxy Nero", "Granite", "Black Galaxy"), ("Star Galaxy", "Granite", "Black Galaxy"),
                             ("G603", "Granite", "Black Galaxy")):
        assert varieties.alias_target(value, typ) == name, value
        assert engine.match(value).canonical == name, value



# --- review (reviewer 2, 11-12) ---------------------------------------------------------------------------------------
def test_a_spaced_or_hyphenated_listing_still_binds_its_code_in_the_exact_tier():
    # L1 (blocking): before C3 'G-603' and 'G 603' bound the seed's 'G603' through the compact projection. The exact
    # tier looks a query up by its compact key against the codes, so they still bind; the code VALUE rule is unchanged
    from stone_pipeline.adapters.tokens import clean_variety
    _, eng = _engine()
    for listing in ("G-603", "G 603", clean_variety("G-603 Granite", "Granite")):
        m = eng.match(listing, block_type="granite")
        assert (m.cid, m.method) == ("g1", "exact"), listing
    assert eng.match("G-630").cid is None                       # still never a near code


def test_the_seed_is_parsed_once_per_backbone_file(tmp_path, monkeypatch):
    # L5: load_all parses the backbone; alias_target's rebuild (every config.db write) reuses that parse while the
    # file is unchanged, and parses again only when the file changes
    import dataclasses
    import json
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.config import settings
    from stone_pipeline.reference.loaders import backbone as bb
    path = tmp_path / "backbone_varieties.json"
    path.write_text(json.dumps({"posts": [{"key": "granite__x_1", "variant": "X", "stone_type": "Granite",
                                           "aliases": ["Ex"]}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS", dataclasses.replace(
        settings.SETTINGS, paths=dataclasses.replace(settings.SETTINGS.paths, backbone_json=path)))
    parses = []
    real = bb.load_backbone
    monkeypatch.setattr(bb, "load_backbone", lambda p=None: parses.append(p) or real(path))
    loaded = bb.load_backbone()                                   # load_all's own parse
    first = alias_stores.scraper_stores(loaded).seed
    assert alias_stores.scraper_stores().seed == first and len(parses) == 1   # alias_target reuses it
    path.write_text(json.dumps({"posts": [{"key": "granite__x_1", "variant": "X", "stone_type": "Granite",
                                           "aliases": ["Ex", "Ecks"]}]}), encoding="utf-8")
    assert alias_stores.scraper_stores().seed["granite__x_1"][0] == ("Ex", "Ecks") and len(parses) == 2
