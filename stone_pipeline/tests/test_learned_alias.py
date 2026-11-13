"""P2-10 C2 (PHASE-2-SPEC WP2.3; P2-10-SCRAPER-PLAN sections 3 and 6): learned aliases live in config.db's
`learned_alias` table, keyed by the variety's identity Key, and only an ACTIVE row is a matcher input.

The table shape is frozen by the scraper manager (11-12): learned_alias(identity_key, value, norm, method, source,
status, first_seen, decided_by, decided_at), PRIMARY KEY (identity_key, norm), norm = core.text.match_key(value),
status in proposed | active | rejected; plus a nullable score REAL (the manager 11-12, PHASE-2-SPEC:176), the proposing
arm's own score on 0..100 where it has one, NULL otherwise and for a migrated row.
  * the matcher's guesses (WriteBack.add_alias) and curate's alias arms write `proposed`: a proposal is not a
    matcher input, and a norm once rejected for a variety is never proposed again;
  * config.db goes from phase 1's v12 to v13 in place (an additive table); a store from before phase 1 stays refused,
    and the phase 1 store migration keeps stamping its own version, 12;
  * the old state/alias_writeback.csv moves in ONCE per env, at the produce's start: a row resolving to a live Key
    that passes the alias boundary (C1) becomes active, decided_by 'migration'; every other row is dropped and listed
    with its reason; the marker in config.db's `migration` table makes every later produce a no-op that never reads
    the csv again.
Hermetic: tmp stores, a hand-built index, the committed vocabulary. No em dashes.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from stone_pipeline import produce
from stone_pipeline.config import decisions_store, store
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.core.text import match_key
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import curate, match_variation
from stone_pipeline.state import writeback as wb
from stone_pipeline.tests._existing_index import existing_index
from stone_pipeline.tests.test_origin_aware_matching import _stage

KEY = "granite__kashmir_golda_1"
CARRARA = "marble__bianco_carrara_C"
READ_COLUMNS = ["identity_key", "value", "norm", "method", "source", "status", "first_seen", "decided_by", "decided_at"]
COLUMNS = READ_COLUMNS + ["score"]


@pytest.fixture
def config_db(tmp_path, monkeypatch):
    """An isolated config.db the config server's boot would leave: present and seeded from sources.yaml (a learned-alias
    write never creates the store)."""
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    store.seed_from_yaml()
    return path


def _rows(path) -> list[dict]:
    with closing(sqlite3.connect(str(path))) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM learned_alias ORDER BY identity_key, norm")]


def _proposal(value, key=KEY, method="fuzzy", source="zucchi", score=None):
    return {"identity_key": key, "value": value, "method": method, "source": source, "score": score}


# --- config.db v13 ---------------------------------------------------------------------------------------------------

def test_the_learned_alias_table_has_the_frozen_shape(config_db):
    with closing(store.open_store()) as conn:
        info = conn.execute("PRAGMA table_info(learned_alias)").fetchall()
        assert [r["name"] for r in info] == COLUMNS
        assert {r["name"]: (r["type"], r["notnull"]) for r in info}["score"] == ("REAL", 0)
        assert [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])] == ["identity_key",
                                                                                                      "norm"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO learned_alias VALUES ('k', 'v', 'v', 'fuzzy', '', 'maybe', 't', NULL, NULL, NULL)")
    assert store.SCHEMA_VERSION == 14 and store.PHASE1_SCHEMA_VERSION == 12


def test_a_phase_1_store_opens_in_place_as_v13_with_an_empty_learned_alias(config_db):
    with closing(store.open_store()) as conn:
        conn.execute("INSERT INTO retired_variation (key, created_at) VALUES ('marble__gone_1', 't')")
        conn.execute("DROP TABLE learned_alias")
        conn.execute(f"PRAGMA user_version = {store.PHASE1_SCHEMA_VERSION}")
        conn.commit()
    with closing(store.open_store()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute("SELECT COUNT(*) FROM learned_alias").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT key FROM retired_variation")] == ["marble__gone_1"]


def test_a_store_from_before_phase_1_is_still_refused(config_db):
    with closing(store.open_store()) as conn:
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
    with pytest.raises(store.ConfigStoreError):
        store.open_store()


def test_the_phase_1_store_migration_stamps_phase_1_s_own_version(tmp_path):
    from stone_pipeline.tests.test_phase1_store_writer import OLD_BLOCK, OLD_SLAB, OLD_TILE, _key_map, _old_config
    from stone_pipeline.phase1 import store_writer
    old = _old_config(tmp_path / "old.db", retired=[OLD_SLAB, OLD_BLOCK, OLD_TILE])
    assert store_writer.migrate_config(old, tmp_path / "new.db", _key_map(), {OLD_SLAB, OLD_BLOCK, OLD_TILE}).aborts == {}
    with closing(sqlite3.connect(str(tmp_path / "new.db"))) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == store.PHASE1_SCHEMA_VERSION


# --- the store: proposed, active, rejected ---------------------------------------------------------------------------

def test_a_proposal_is_stored_as_proposed_and_is_not_active(config_db):
    assert decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx", score=91.5)]) == 1
    [row] = _rows(config_db)
    assert {k: row[k] for k in ("identity_key", "value", "norm", "method", "source", "status", "decided_by",
                                "score")} == {
        "identity_key": KEY, "value": "Kashmir Goldx", "norm": match_key("Kashmir Goldx"), "method": "fuzzy",
        "source": "zucchi", "status": "proposed", "decided_by": None, "score": 91.5}
    assert row["first_seen"] and row["decided_at"] is None
    assert decisions_store.active_learned_aliases() == []


def test_a_repeat_proposal_of_the_same_norm_adds_nothing(config_db):
    decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx")])
    first = _rows(config_db)
    assert decisions_store.propose_learned_aliases([_proposal("KASHMIR  goldx", source="sentas")]) == 0
    assert _rows(config_db) == first


def test_approving_a_proposal_makes_it_active(config_db):
    decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx")])
    assert decisions_store.decide_learned_alias(KEY, "kashmir goldx", "active", "operator") is True
    [row] = decisions_store.active_learned_aliases()
    assert row["identity_key"] == KEY and row["value"] == "Kashmir Goldx" and row["status"] == "active"
    assert row["decided_by"] == "operator" and row["decided_at"]
    assert set(row) == set(READ_COLUMNS)                        # the reader's shape is unchanged by the score


def test_a_rejected_norm_is_never_proposed_again(config_db):
    decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx")])
    assert decisions_store.decide_learned_alias(KEY, "Kashmir Goldx", "rejected", "operator") is True
    assert decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx")]) == 0
    assert [r["status"] for r in _rows(config_db)] == ["rejected"]
    assert decisions_store.active_learned_aliases() == []


def test_deciding_an_unknown_alias_changes_nothing(config_db):
    assert decisions_store.decide_learned_alias(KEY, "Never Proposed", "active", "operator") is False
    with pytest.raises(ValueError):
        decisions_store.decide_learned_alias(KEY, "Kashmir Goldx", "proposed", "operator")


def test_the_active_reader_is_sorted_and_creates_no_store(tmp_path, monkeypatch, config_db):
    absent = tmp_path / "absent.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(absent))
    assert decisions_store.active_learned_aliases() == [] and not absent.exists()
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(config_db))
    decisions_store.propose_learned_aliases([_proposal("Zeta"), _proposal("Alpha"), _proposal("Mid", key="a__k_0")])
    for value, key in (("Zeta", KEY), ("Alpha", KEY), ("Mid", "a__k_0")):
        decisions_store.decide_learned_alias(key, value, "active", "operator")
    assert [(r["identity_key"], r["norm"]) for r in decisions_store.active_learned_aliases()] == [
        ("a__k_0", "mid"), (KEY, "alpha"), (KEY, "zeta")]


# --- the matcher: active rows only, proposals from its guesses -------------------------------------------------------

def _built_stage(monkeypatch):
    """VariationStage.build over one hand-built index (candidate 'a' = KEY), so the learned rows it applies are seen."""
    index = CandidateIndex()
    index.add("a", "Kashmir Golda", surfaces=[], block_type="granite")
    monkeypatch.setattr(match_variation, "build_variation_index", lambda table, backbone, origin_map=None: index)
    ref = SimpleNamespace(variants={"slab": SimpleNamespace(by_id={"a": SimpleNamespace(
        key=KEY, variation_id="a", name="Kashmir Golda", archived=False)})}, backbone=None, origin_map=None)
    match_variation.VariationStage.build(ref)
    return index


def test_a_proposed_learned_alias_is_not_a_matcher_input(config_db, monkeypatch):
    decisions_store.propose_learned_aliases([_proposal("Totally Other Name")])
    assert "Totally Other Name" not in _built_stage(monkeypatch).candidates["a"].surfaces


def test_an_active_learned_alias_reaches_its_variety_by_key(config_db, monkeypatch):
    decisions_store.propose_learned_aliases([_proposal("Totally Other Name")])
    decisions_store.decide_learned_alias(KEY, "Totally Other Name", "active", "operator")
    assert "Totally Other Name" in _built_stage(monkeypatch).candidates["a"].surfaces


def test_the_matcher_no_longer_reads_the_alias_writeback_csv(config_db, monkeypatch, tmp_path):
    def refuse(*a, **k):
        raise AssertionError("the matcher read alias_writeback.csv")
    monkeypatch.setattr(wb, "load_alias_writeback_records", refuse)
    _built_stage(monkeypatch)


def test_a_fuzzy_bind_is_proposed_on_its_variety_key(config_db):
    idx = CandidateIndex()
    idx.add("a", "Kashmir Golda", surfaces=[], block_type="granite", origins={"BR"})
    stage = match_variation.VariationStage(
        ref=SimpleNamespace(**{**vars(_stage().ref), "variants": {"slab": SimpleNamespace(
            by_id={"a": SimpleNamespace(key=KEY, archived=False)})}}),
        engines={"slab": VariationEngine(idx, auto_accept=90, review_floor=80)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Goldx",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_key == KEY and row.variation_method == "fuzzy"
    assert stage.writeback.flush() == 1
    assert [(r["identity_key"], r["value"], r["method"], r["source"], r["status"]) for r in _rows(config_db)] == [
        (KEY, "Kashmir Goldx", "fuzzy", "zucchi", "proposed")]
    [row] = _rows(config_db)
    assert row["score"] is not None and 80 <= row["score"] < 100       # the fuzzy tier's own score, 0..100
    assert decisions_store.active_learned_aliases() == []


# --- curate: its alias arms propose, they never reach the import delta ----------------------------------------------

@pytest.fixture(scope="module")
def ref():
    from stone_pipeline.reference import loaders
    return loaders.load_all()


def test_curate_s_learned_alias_is_a_proposal_not_an_import_row(monkeypatch, ref):
    monkeypatch.setattr(curate, "load_existing", lambda: existing_index((CARRARA, "Bianco Carrara", "")))
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Bianco Carara", raw_type="Marble",
                       type_name="Marble", variation_id="C", variation_key=CARRARA,
                       variation_name="Bianco Carrara", variation_method="fuzzy")
    res = curate.build_curation([row], ref)
    assert res.learned_proposals == [{"identity_key": CARRARA, "value": "Bianco Carara", "method": "curate_confirmed",
                                      "source": "zucchi", "score": None}]
    assert not hasattr(res, "alias_additions")


def test_write_curation_stores_curate_s_proposals(config_db, monkeypatch):
    from stone_pipeline.stages.curate import output
    from stone_pipeline.stages.curate.context import CurationResult
    stored: list = []
    monkeypatch.setattr(output.decisions_store, "propose_learned_aliases", lambda rows: stored.extend(rows) or 0)
    monkeypatch.setattr(output, "_write_files", lambda result, rows: None)
    proposals = [{"identity_key": CARRARA, "value": "Bianco Carara", "method": "curate_confirmed", "source": "zucchi",
                  "score": None}]
    output.write_curation(CurationResult(learned_proposals=proposals), [])
    assert stored == proposals


# --- the one-time migration of alias_writeback.csv -------------------------------------------------------------------

LIVE = "granite__patagonia_white_72e1"
OTHER = "granite__white_linen_60ae"
GONE = "granite__retired_one_0001"


def _ledger(path):
    """The ledger the migration resolves against; built once per path, so a second produce sees the same ledger."""
    if path.exists():
        return path
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE variation (key TEXT PRIMARY KEY, type TEXT, name TEXT, medusa_id TEXT, "
                     "in_full INTEGER, state TEXT)")
        conn.executemany("INSERT INTO variation VALUES (?, ?, ?, ?, ?, ?)", [
            (LIVE, "Granite", "Patagonia White", "01PATAGONIA", 1, "dirty"),
            (OTHER, "Granite", "White Linen", "01LINEN", 1, "synced"),
            (GONE, "Granite", "Retired One", "01GONE", 1, "retiring")])
        conn.commit()
    return path


def _writeback(path, rows):
    with path.open("w", encoding="utf-8", newline="") as h:
        w = csv.writer(h)
        w.writerow(["variation_id", "alias", "method"])
        w.writerows(rows)
    return path


def _migrate(tmp_path, records):
    from stone_pipeline.aliases import migrate_learned
    return migrate_learned.run_once(writeback_path=_writeback(tmp_path / "alias_writeback.csv", records),
                                    ledger_path=_ledger(tmp_path / "ledger.db"), review_dir=tmp_path / "review")


def _dropped(tmp_path) -> list[dict]:
    from stone_pipeline.aliases import migrate_learned
    with (tmp_path / "review" / migrate_learned.DROPPED_REVIEW).open(encoding="utf-8-sig", newline="") as h:
        return list(csv.DictReader(h))


def test_the_migration_activates_each_row_on_a_live_key_that_passes_the_boundary(config_db, tmp_path):
    report = _migrate(tmp_path, [("01PATAGONIA", "PATAGONIA", "phonetic"),        # by the ledger's medusa_id
                                 (OTHER, "China White", "fuzzy")])                 # already a new Key
    assert report == {"active": 2, "dropped": 0}
    assert [r["score"] for r in _rows(config_db)] == [None, None]          # a migrated row carries no score
    assert [(r["identity_key"], r["value"], r["method"], r["status"], r["decided_by"], r["source"])
            for r in decisions_store.active_learned_aliases()] == [
        (LIVE, "PATAGONIA", "phonetic", "active", "migration", ""),
        (OTHER, "China White", "fuzzy", "active", "migration", "")]


def test_the_migration_drops_and_lists_every_other_row_with_its_reason(config_db, tmp_path):
    report = _migrate(tmp_path, [
        ("01NOWHERE", "Some Name", "fuzzy"),            # resolves to no Key
        ("01GONE", "Old Spelling", "fuzzy"),            # a retiring Key is not live
        ("01PATAGONIA", "Patagonia White", "fuzzy"),    # its own name (C1 boundary)
        ("01PATAGONIA", "White Linen", "fuzzy"),        # another same-type variety's name (C1 boundary)
        ("01PATAGONIA", "G603", "fuzzy"),               # a code, never an alias
        ("01PATAGONIA", "Legacy Pick", "exact"),        # not a learned guess
        ("01PATAGONIA", "PATAGONIA", "phonetic"),
        ("01PATAGONIA", "patagonia", "fuzzy"),          # the same norm again
    ])
    assert report == {"active": 1, "dropped": 7}
    assert [(r["variation_id"], r["alias"], r["reason"]) for r in _dropped(tmp_path)] == [
        ("01GONE", "Old Spelling", "no_live_key"),
        ("01NOWHERE", "Some Name", "no_live_key"),
        ("01PATAGONIA", "G603", "is_code"),
        ("01PATAGONIA", "Legacy Pick", "not_learned"),
        ("01PATAGONIA", "Patagonia White", "own_name"),
        ("01PATAGONIA", "White Linen", "same_type_name"),
        ("01PATAGONIA", "patagonia", "duplicate"),
    ]


def test_a_second_produce_s_migration_is_a_no_op(config_db, tmp_path):
    assert _migrate(tmp_path, [("01PATAGONIA", "PATAGONIA", "phonetic")]) == {"active": 1, "dropped": 0}
    before = _rows(config_db)
    assert _migrate(tmp_path, [("01PATAGONIA", "PATAGONIA", "phonetic"), (OTHER, "China White", "fuzzy")]) is None
    assert _rows(config_db) == before


def test_a_store_holding_the_migration_marker_never_reads_the_csv(config_db, tmp_path, monkeypatch):
    from stone_pipeline.aliases import migrate_learned
    _migrate(tmp_path, [])
    monkeypatch.setattr(migrate_learned, "load_alias_writeback_records",
                        lambda path: (_ for _ in ()).throw(AssertionError("the csv was read again")))
    assert _migrate(tmp_path, [("01PATAGONIA", "PATAGONIA", "phonetic")]) is None


def test_an_absent_csv_migrates_nothing_and_still_marks_the_store(config_db, tmp_path):
    from stone_pipeline.aliases import migrate_learned
    assert migrate_learned.run_once(writeback_path=tmp_path / "absent.csv", ledger_path=_ledger(tmp_path / "l.db"),
                                    review_dir=tmp_path / "review") == {"active": 0, "dropped": 0}
    assert _migrate(tmp_path, [("01PATAGONIA", "PATAGONIA", "phonetic")]) is None


def test_a_brand_new_environment_records_an_empty_migration(config_db, tmp_path):
    # Scraper's L2 on SC #650: no ledger and no csv yet (a new env's first produce) is an EMPTY migration, recorded;
    # the ledger is read only to resolve records, and there are none
    from stone_pipeline.aliases import migrate_learned
    assert migrate_learned.run_once(writeback_path=tmp_path / "absent.csv", ledger_path=tmp_path / "no-ledger.db",
                                    review_dir=tmp_path / "review") == {"active": 0, "dropped": 0}
    assert decisions_store.learned_migration_done(migrate_learned.MIGRATION)


def test_records_with_no_ledger_to_resolve_them_fail_loud(config_db, tmp_path):
    from stone_pipeline.aliases import migrate_learned
    with pytest.raises(FileNotFoundError, match="no ledger"):
        migrate_learned.run_once(writeback_path=_writeback(tmp_path / "w.csv", [("01PATAGONIA", "PATAGONIA", "phonetic")]),
                                 ledger_path=tmp_path / "no-ledger.db", review_dir=tmp_path / "review")
    assert not decisions_store.learned_migration_done(migrate_learned.MIGRATION)


def test_the_produce_migrates_after_the_inputs_and_before_the_build(monkeypatch):
    from stone_pipeline.aliases import migrate_learned
    from stone_pipeline.ledger import snapshot, writethrough
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_mirror_archived", lambda: None)
    monkeypatch.setattr(writethrough, "enabled", lambda: True)
    monkeypatch.setattr(migrate_learned, "run_once", lambda **k: calls.append("migrate"))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: 0)
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: calls.append("build") or 1)
    monkeypatch.setattr(produce, "_finalize_control_plane", lambda stage: None)
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)
    produce.run(None, "inventory")
    assert calls == ["fetch", "migrate", "build"]


# --- a proposal never leaves a store that hides sources.yaml (CI's `certify all`, 12 Nov) ------------------------------

def test_a_proposal_with_no_config_db_refuses_and_creates_no_file(tmp_path, monkeypatch):
    # the store is created only by the config server's boot (restore from S3, then seed); a write that created it
    # would hide sources.yaml from load_sources (CI's certify found no sources) and, on the service, make the boot skip
    # its restore
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    with pytest.raises(store.ConfigStoreError, match="config.db absent"):
        decisions_store.propose_learned_aliases([_proposal("Kashmir Goldx")])
    with pytest.raises(store.ConfigStoreError, match="config.db absent"):
        decisions_store.record_learned_migration("m", [])
    assert not path.exists()


@pytest.fixture(scope="module")
def module_config_db():
    import os
    return os.environ.get("BLOKPORT_CONFIG_DB")


def test_a_module_scoped_fixture_sees_an_isolated_config_store(module_config_db):
    # a module-scoped fixture runs before the function-scoped isolation; it must not reach the checkout's config.db
    from pathlib import Path
    from stone_pipeline.config.settings import SETTINGS
    assert module_config_db and Path(module_config_db) != SETTINGS.paths.workspace_root / "config.db"
    assert not Path(module_config_db).exists()


# --- config.db v14: norms rekeyed to alias_key (Mas, 13 Nov) ---------------------------------------------------------

_REKEY = getattr(store, "learned_norms_to_rekey", None)
_INSERT = ("INSERT INTO learned_alias (identity_key, value, norm, method, source, status, first_seen) "
           "VALUES (?, ?, ?, 'fuzzy', '', 'active', 't')")


def _as_v13(path, rows):
    """The store as a v13 open left it: `rows` (Key, value, norm) stored, the rekey not yet recorded."""
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executemany(_INSERT, rows)
        conn.execute("DELETE FROM migration WHERE name = 'learned_norm_alias_key_v1'")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()


def test_the_rekey_moves_a_non_latin_norm_once_and_leaves_every_latin_norm(config_db):
    assert _REKEY is not None, "config.store has no learned_norms_to_rekey (the alias_key rekey)"
    _as_v13(config_db, [(KEY, "Wratza 维纳多", match_key("Wratza 维纳多")),
                        (KEY, "Kashmir Gold", match_key("Kashmir Gold"))])
    with closing(store.open_store()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert _REKEY(conn) == []                                    # a re-run would move 0 rows
    assert [(r["value"], r["norm"]) for r in _rows(config_db)] == [
        ("Kashmir Gold", "kashmir gold"), ("Wratza 维纳多", "wratza 维纳多")]


def test_a_rekey_onto_a_held_norm_refuses_the_open_and_changes_nothing(config_db):
    assert _REKEY is not None, "config.store has no learned_norms_to_rekey (the alias_key rekey)"
    rows = [(KEY, "Wratza 维纳多", "wratza"), (KEY, "WRATZA 维纳多", "wratza 维纳多")]
    _as_v13(config_db, rows)
    with pytest.raises(store.ConfigStoreError, match="rekey"):
        store.open_store()
    assert sorted((r["identity_key"], r["value"], r["norm"]) for r in _rows(config_db)) == sorted(rows)
    with closing(sqlite3.connect(str(config_db))) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 13


def test_a_non_latin_learned_alias_is_proposed_under_its_own_key(config_db):
    # match_key reads a pure-CJK value as empty (refused); its alias_key keeps the script
    assert decisions_store.propose_learned_aliases([_proposal("巴比伦金")]) == 1
    assert [(r["value"], r["norm"]) for r in _rows(config_db)] == [("巴比伦金", "巴比伦金")]
