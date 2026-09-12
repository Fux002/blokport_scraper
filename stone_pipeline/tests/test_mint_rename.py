"""Mint + rename: the operator corrects a proposed variety's NAME while minting it.

Contract under test (API -> store -> curate):
  * PUT /config/v1/review/variants/<scraped> {"action":"mint","name":X} stores X as seed_name; the echo and
    the pending card read it back. X is refused (400) when (type, X) already exists -- identity is the atomic
    (type, name) pair, so the same name under ANOTHER type is legal.
  * reject / alias ignore `name`; a rename to the same spelling stores nothing (no self-alias).
  * clearing a decision by the RENAMED name drops the row keyed by the scraped spelling (no zombie).
  * curate mints the variety under X (Name + Key) with the scraped spelling as its alias, keeps the observed
    colours/finishes and the seed colour, and folds two spellings renamed to one X into ONE variety.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from stone_pipeline.config import decisions_store, server, varieties
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, decisions
from stone_pipeline.tests.test_mint_type_authority import _empty_imports, _typed_gap_row

SCRAPED = "Honey Onyx"          # the 1-token guard keeps the type word, so the clean name IS the scraped one
RENAMED = "Honey"


# -- fixtures -------------------------------------------------------------------

@pytest.fixture
def config_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    return tmp_path / "config.db"


def _card(stone_type: str = "onyx") -> None:
    decisions.write_confirm_file([{"confirm": "", "variant": SCRAPED, "stone_type": stone_type,
                                   "color": "yellow", "nearest_existing": "", "score": 0.5, "model_prob": 0.5}])


def _vocab(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(varieties, "_rows", lambda q=None: rows)


def _row(name: str, key: str, stone_type: str = "Onyx", **attrs) -> CanonicalRow:
    base = _typed_gap_row(name, stone_type)
    gaps = [g.model_copy(update={"surrogate_key": key}) for g in base.tree_gaps]
    return base.model_copy(update={"surrogate_key": key, "tree_gaps": gaps, **attrs})


def _isolate_curate(monkeypatch, confirm: dict, seed_names: dict, seed_colors: dict | None = None) -> None:
    monkeypatch.setattr(curate, "load_all_existing", lambda: _empty_imports())
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    monkeypatch.setattr(decisions, "load_confirm_decisions", lambda: confirm)
    monkeypatch.setattr(decisions, "load_variety_seed_names", lambda: seed_names)
    if seed_colors is not None:
        monkeypatch.setattr(decisions, "load_variety_seed_colors", lambda: seed_colors)


def _new(res, branch: str) -> list[dict]:
    return list(res.new_variants[branch])


# -- API -----------------------------------------------------------------------


# -- store ---------------------------------------------------------------------

def test_clear_by_the_renamed_name_drops_the_scraped_row(config_db):
    decisions_store.set_variety_decision(SCRAPED, "mint", seed_type="Onyx", seed_name=RENAMED)
    # un-mint arrives with the variety NAME Medusa knows (the renamed one), not the scraped spelling
    assert decisions_store.clear_variety_decision(RENAMED) == 1
    assert decisions_store.confirm_map() == {}
    assert decisions_store.variety_seed_names() == {}


def test_old_schema_gains_the_seed_name_column(config_db):
    with sqlite3.connect(config_db) as conn:
        conn.execute("CREATE TABLE variety_decision (variant_norm TEXT PRIMARY KEY, variant TEXT NOT NULL, "
                     "action TEXT NOT NULL, alias_of TEXT, seed_color TEXT, seed_type TEXT, seed_country TEXT, "
                     "decided_at TEXT NOT NULL)")
    decisions_store.variety_actions()                          # open_store -> _migrate
    with sqlite3.connect(config_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(variety_decision)")}
    assert "seed_name" in cols


# -- curate --------------------------------------------------------------------

def test_mint_under_seed_name_creates_the_renamed_variety_with_the_scraped_alias(monkeypatch):
    _isolate_curate(monkeypatch, {("", "honey onyx"): "yes"}, {("", "honey onyx"): RENAMED})
    res = curate.build_curation([_row(SCRAPED, "t1", color_name="yellow", finish_name="polished")],
                                loaders.load_all())
    for b in ("slab", "block", "tile"):
        rows = _new(res, b)
        assert [r["Name"] for r in rows] == [RENAMED], (b, rows)
        assert "_onyx_honey_" in rows[0]["Key"], rows[0]["Key"]
        assert SCRAPED in rows[0]["Aliases"].split("|"), rows[0]["Aliases"]
    bb = [r for r in res.backbone_new["slab"] if r["variant"] == RENAMED]
    assert len(bb) == 1
    assert bb[0]["color"] == ["Yellow"]                        # observed colour survives the rename
    assert "Polished" in bb[0]["finishes"]                     # observed finish folded into the type defaults


def test_seed_colour_survives_the_rename(monkeypatch):
    _isolate_curate(monkeypatch, {("", "honey onyx"): "yes"}, {("", "honey onyx"): RENAMED}, {("", "honey onyx"): "Gold"})
    res = curate.build_curation([_row(SCRAPED, "t1", color_name="yellow")], loaders.load_all())
    bb = [r for r in res.backbone_new["slab"] if r["variant"] == RENAMED]
    assert bb and bb[0]["color"] == ["Gold"]


def test_two_spellings_renamed_to_one_name_mint_one_variety(monkeypatch):
    _isolate_curate(monkeypatch, {("", "honey onyx"): "yes", ("", "honig onyx"): "yes"},
                    {("", "honey onyx"): RENAMED, ("", "honig onyx"): RENAMED})
    res = curate.build_curation([_row(SCRAPED, "t1"), _row("Honig Onyx", "t2")], loaders.load_all())
    for b in ("slab", "block", "tile"):
        rows = _new(res, b)
        assert [r["Name"] for r in rows] == [RENAMED], (b, rows)
        assert set(rows[0]["Aliases"].split("|")) >= {SCRAPED, "Honig Onyx"}, rows[0]["Aliases"]


def test_same_name_mints_of_different_types_keep_their_own_aliases(monkeypatch):
    # Identity is (type, name): an Onyx 'Honey' and a Marble 'Honey' minted in ONE run must each carry only
    # their own scraped spelling. Leaking a spelling across types would make it an ambiguous surface of two
    # owners and send its products to review on the next produce.
    _isolate_curate(monkeypatch, {("", "honey onyx"): "yes", ("", "honey marble"): "yes"},
                    {("", "honey onyx"): RENAMED, ("", "honey marble"): RENAMED})
    res = curate.build_curation([_row(SCRAPED, "t1"), _row("Honey Marble", "t2", stone_type="Marble")],
                                loaders.load_all())
    rows = _new(res, "slab")
    by_type = {("_onyx_" in r["Key"] and "Onyx") or ("_marble_" in r["Key"] and "Marble"): r for r in rows}
    assert set(by_type) == {"Onyx", "Marble"}, rows
    assert by_type["Onyx"]["Aliases"] == SCRAPED
    assert by_type["Marble"]["Aliases"] == "Honey Marble"


def test_mint_without_a_seed_name_is_unchanged(monkeypatch):
    _isolate_curate(monkeypatch, {("", "honey onyx"): "yes"}, {})
    res = curate.build_curation([_row(SCRAPED, "t1")], loaders.load_all())
    rows = _new(res, "slab")
    assert [r["Name"] for r in rows] == [SCRAPED]
    assert rows[0]["Aliases"] == ""


# -- API: a rename is a statement whose name differs from the spelling ---------------------------------

def test_statement_with_a_corrected_name_is_a_mint_plus_rename(config_db, monkeypatch):
    _card(); _vocab(monkeypatch, [])
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": SCRAPED, "name": RENAMED, "type": "Onyx"})
    assert code == 200 and body["result"] == "minted" and body["level"] == "global"
    assert decisions.load_variety_seed_names() == {("", "honey onyx"): RENAMED}       # the spelling's meaning for everyone
    assert decisions_store.scoped_aliases() == {}                                # curate attaches the alias globally


def test_statement_naming_an_existing_type_name_pair_binds_instead_of_minting(config_db, monkeypatch):
    _card(); _vocab(monkeypatch, [{"name": RENAMED, "stone_type": "Onyx"}])
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": SCRAPED, "name": RENAMED, "type": "Onyx"})
    assert code == 200 and body["result"] == "bound"
    assert decisions.load_variety_seed_names() == {}
    assert decisions_store.confirm_map() == {}                 # bound = nothing minted, not a half-mint


def test_statement_naming_the_pair_under_a_different_type_mints(config_db, monkeypatch):
    _card(); _vocab(monkeypatch, [{"name": RENAMED, "stone_type": "Marble"}])
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": SCRAPED, "name": RENAMED, "type": "Onyx"})
    assert code == 200 and body["result"] == "minted"


def test_statement_with_the_same_spelling_is_a_plain_mint(config_db, monkeypatch):
    _card(); _vocab(monkeypatch, [])
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": SCRAPED, "name": "honey  ONYX", "type": "Onyx"})
    assert code == 200 and body["result"] == "minted"
    assert decisions.load_variety_seed_names() == {}
    assert decisions_store.confirm_map() == {("", "honey onyx"): "yes"}
    assert decisions_store.scoped_aliases() == {}


# -- a statement keyed on a spelling that carries a type word must still fire in curate ----------------

def test_decision_keyed_on_a_type_word_spelling_mints_and_seeds(monkeypatch):
    # the card's scraped spelling is 'Bianco White Marble'; curate's cleaned identity is 'Bianco White'.
    # A statement stores mint / rename / colour under the SPELLING: curate must find them.
    _isolate_curate(monkeypatch, {("", "bianco white marble"): "yes"}, {("", "bianco white marble"): "Bianco White"},
                    {("", "bianco white marble"): "White"})
    res = curate.build_curation([_row("Bianco White Marble", "b1", stone_type="Marble")], loaders.load_all())
    rows = _new(res, "slab")
    assert [r["Name"] for r in rows] == ["Bianco White"], rows
    # no alias needed: the product's cleaned identity 'Bianco White' binds the new variety by exact name
    assert rows[0]["Aliases"] == ""
    assert res.backbone_new["slab"][0]["color"] == ["White"]


# -- vendor scope + type-aware unmint (found in the full re-review) ------------------------------------

def _statement_setup(tmp_path, monkeypatch):
    from stone_pipeline.tests.test_mint_rename_two_produce import _paths, _write_export, EXISTING_KEY
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    paths = _paths(tmp_path)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    monkeypatch.setattr(varieties, "_rows", lambda q=None: [])
    _write_export(paths.export_file, [{"Id": "var_alpine", "Key": EXISTING_KEY, "Name": "Alpine"}])
    return loaders.load_all()


def test_the_first_mint_on_a_spelling_decides_every_vendor_and_a_vendor_may_still_differ(tmp_path, monkeypatch):
    # TWO LEVELS. zucchi's statement "Honey Onyx is Honey (Onyx)" is the first mint on that spelling, so it is
    # what the spelling MEANS: polonine's identical spelling mints the same Honey (no card, no duplicate).
    ref = _statement_setup(tmp_path, monkeypatch)
    assert decisions_store.decide("zucchi", "Honey Onyx", "Honey", "Onyx", color="Yellow")["level"] == "global"
    zucchi = _typed_gap_row("Honey Onyx", "Onyx")
    polonine = _typed_gap_row("Honey Onyx", "Onyx").model_copy(update={"src_site": "polonine"})
    res = curate.build_curation([zucchi, polonine], ref)
    assert [r["Name"] for r in res.new_variants["slab"]] == ["Honey"]
    assert not any(c.get("variant") == "Honey Onyx" for c in res.pending_confirm)
    # polonine then says its Honey Onyx is a DIFFERENT new stone: that is polonine's own level, beside the global
    assert decisions_store.decide("polonine", "Honey Onyx", "Honey Polonia", "Onyx", color="Beige")["level"] == "vendor"
    res = curate.build_curation([zucchi, polonine], ref)
    names = sorted(r["Name"] for r in res.new_variants["slab"])
    assert names == ["Honey", "Honey Polonia"], names
    assert decisions_store.scoped_aliases()[("polonine", "honey onyx")] == ("Honey Polonia", "Onyx")
    bb = {r["variant"]: r["color"] for r in res.backbone_new["slab"]}
    assert bb["Honey"] == ["Yellow"] and bb["Honey Polonia"] == ["Beige"]   # each level keeps its own colour


def test_a_global_mint_decision_still_decides_every_vendor(tmp_path, monkeypatch):
    ref = _statement_setup(tmp_path, monkeypatch)
    decisions_store.set_variety_decision("Honey Onyx", "mint", seed_type="Onyx", seed_name="Honey")   # '' scope
    rows = [_typed_gap_row("Honey Onyx", "Onyx"),
            _typed_gap_row("Honey Onyx", "Onyx").model_copy(update={"src_site": "polonine"})]
    res = curate.build_curation(rows, ref)
    assert [r["Name"] for r in res.new_variants["slab"]] == ["Honey"]
    assert res.pending_confirm == []


def test_unmint_clears_by_type_and_name(config_db):
    decisions_store.set_variety_decision("Honey Onyx", "mint", seed_type="Onyx", seed_name="Honey")
    decisions_store.set_variety_decision("Honey Marble", "mint", seed_type="Marble", seed_name="Honey")
    decisions_store.set_variety_decision("Honey", "mint", seed_type="Marble")            # plain mint, Marble
    assert decisions_store.clear_variety_decision("Honey", "onyx") == 1                # the Key type-slug form
    assert decisions_store.variety_seed_names() == {("", "honey marble"): "Honey"}
    assert decisions_store.confirm_map() == {("", "honey marble"): "yes", ("", "honey"): "yes"}
    assert decisions_store.clear_variety_decision("Honey", "Marble") == 2               # canonical form
    assert decisions_store.confirm_map() == {}


def test_unmint_without_a_type_keeps_legacy_name_only_semantics(config_db):
    decisions_store.set_variety_decision("Honey Onyx", "mint", seed_type="Onyx", seed_name="Honey")
    decisions_store.set_variety_decision("Honey Marble", "mint", seed_type="Marble", seed_name="Honey")
    assert decisions_store.clear_variety_decision("Honey") == 2


def _matched_row(scraped: str, variety: str, stone_type: str = "Granite", **attrs) -> CanonicalRow:
    """A scrape the matcher RESOLVED to an existing variety: it carries a variation_key and NO
    missing_variation gap, so curate's gap loop skipped it before the operator-override fix."""
    return CanonicalRow(src_site="marenostone", surrogate_key="m1", variety_match_key=scraped,
                        raw_type=stone_type, variation_name=variety,
                        variation_key=f"slab_{stone_type.lower()}_matched_x",
                        variation_method="exact_name", tree_gaps=[], **attrs)


def test_a_matched_row_is_overridden_by_an_operator_mint_rename(monkeypatch):
    # The persistent decision_gap: a scraped spelling the matcher resolved to an existing variety (no gap)
    # was skipped, so the operator's "this is really a NEW variety" never applied. An explicit mint+rename on
    # a matched row must still mint the new variety (the scraped spelling becomes its alias, binds next run).
    _isolate_curate(monkeypatch, {("", "brown granite"): "yes"}, {("", "brown granite"): "Chocolate Classic"})
    res = curate.build_curation([_matched_row("Brown Granite", "Brown Granite", color_name="brown")],
                                loaders.load_all())
    for b in ("slab", "block", "tile"):
        rows = _new(res, b)
        assert [r["Name"] for r in rows] == ["Chocolate Classic"], (b, rows)
        assert "_granite_chocolate_classic_" in rows[0]["Key"], rows[0]["Key"]
        assert "Brown Granite" in rows[0]["Aliases"].split("|"), rows[0]["Aliases"]


def test_a_matched_row_without_a_decision_keeps_its_match(monkeypatch):
    # No regression: a matched row with NO operator decision mints nothing -- the matcher's bind stands,
    # exactly as before the override.
    _isolate_curate(monkeypatch, {}, {})
    res = curate.build_curation([_matched_row("Brown Granite", "Brown Granite")], loaders.load_all())
    for b in ("slab", "block", "tile"):
        assert _new(res, b) == [], (b, _new(res, b))
