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
    monkeypatch.setattr(curate, "load_existing", lambda b, *a, **k: _empty_imports()[b])
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
    _isolate_curate(monkeypatch, {"honey onyx": "yes"}, {"honey onyx": RENAMED})
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
    _isolate_curate(monkeypatch, {"honey onyx": "yes"}, {"honey onyx": RENAMED}, {"honey onyx": "Gold"})
    res = curate.build_curation([_row(SCRAPED, "t1", color_name="yellow")], loaders.load_all())
    bb = [r for r in res.backbone_new["slab"] if r["variant"] == RENAMED]
    assert bb and bb[0]["color"] == ["Gold"]


def test_two_spellings_renamed_to_one_name_mint_one_variety(monkeypatch):
    _isolate_curate(monkeypatch, {"honey onyx": "yes", "honig onyx": "yes"},
                    {"honey onyx": RENAMED, "honig onyx": RENAMED})
    res = curate.build_curation([_row(SCRAPED, "t1"), _row("Honig Onyx", "t2")], loaders.load_all())
    for b in ("slab", "block", "tile"):
        rows = _new(res, b)
        assert [r["Name"] for r in rows] == [RENAMED], (b, rows)
        assert set(rows[0]["Aliases"].split("|")) >= {SCRAPED, "Honig Onyx"}, rows[0]["Aliases"]


def test_same_name_mints_of_different_types_keep_their_own_aliases(monkeypatch):
    # Identity is (type, name): an Onyx 'Honey' and a Marble 'Honey' minted in ONE run must each carry only
    # their own scraped spelling. Leaking a spelling across types would make it an ambiguous surface of two
    # owners and send its products to review on the next produce.
    _isolate_curate(monkeypatch, {"honey onyx": "yes", "honey marble": "yes"},
                    {"honey onyx": RENAMED, "honey marble": RENAMED})
    res = curate.build_curation([_row(SCRAPED, "t1"), _row("Honey Marble", "t2", stone_type="Marble")],
                                loaders.load_all())
    rows = _new(res, "slab")
    by_type = {("_onyx_" in r["Key"] and "Onyx") or ("_marble_" in r["Key"] and "Marble"): r for r in rows}
    assert set(by_type) == {"Onyx", "Marble"}, rows
    assert by_type["Onyx"]["Aliases"] == SCRAPED
    assert by_type["Marble"]["Aliases"] == "Honey Marble"


def test_mint_without_a_seed_name_is_unchanged(monkeypatch):
    _isolate_curate(monkeypatch, {"honey onyx": "yes"}, {})
    res = curate.build_curation([_row(SCRAPED, "t1")], loaders.load_all())
    rows = _new(res, "slab")
    assert [r["Name"] for r in rows] == [SCRAPED]
    assert rows[0]["Aliases"] == ""


# -- API: a rename is a statement whose name differs from the spelling ---------------------------------

def test_statement_with_a_corrected_name_is_a_mint_plus_rename(config_db, monkeypatch):
    _card(); _vocab(monkeypatch, [])
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": SCRAPED, "name": RENAMED, "type": "Onyx"})
    assert code == 200 and body["result"] == "minted"
    assert decisions.load_variety_seed_names() == {"honey onyx": RENAMED}
    assert decisions_store.scoped_aliases()[("zucchi", "honey onyx")] == (RENAMED, "Onyx")


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
    assert decisions_store.confirm_map() == {"honey onyx": "yes"}
    assert decisions_store.scoped_aliases() == {}
