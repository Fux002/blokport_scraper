"""The legacy decision tables migrate into `statement` ONCE, row for row, and the object built from the
statements equals the object the legacy projection built (the dual read, DECISION_MODEL_DESIGN.md section 4)."""

from __future__ import annotations

import sqlite3

from stone_pipeline.config import decisions_store, store
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.matching import projections as proj


def _norm(s):
    return proj.norm(s or "")


def _legacy_db(path) -> None:
    """A store with legacy rows: a global mint with seeds, a vendor-level mint with its binding, a global
    reject, a bare vendor binding, a vendor origin on the binding's target and a widened origin on a variety
    with no statement (the origin-queue confirmation)."""
    store.open_store(path).close()                                # real schema; the fresh-store mark is set
    conn = sqlite3.connect(path)
    conn.executescript(
        "PRAGMA user_version = 3;"                                # a database from before the statement table
        "DELETE FROM migration WHERE name = 'statements_v1';"
        "INSERT INTO variety_decision (source, variant_norm, variant_display, action, seed_color, seed_type, "
        "seed_country, seed_name, asked_by, decided_at) VALUES "
        "('', 'honey onyx', 'Honey Onyx', 'mint', 'Gold', 'Onyx', 'IR', 'Honey', 'zucchi', 't'),"
        "('marenostone', 'brown granite', 'Brown Granite', 'mint', NULL, 'Granite', NULL, 'Chocolate Classic', 'marenostone', 't'),"
        "('', 'junk code', 'Junk Code', 'reject', NULL, NULL, NULL, NULL, '', 't');"
        "INSERT INTO scoped_alias (source, variant_norm, variant_display, alias_of, seed_type, decided_at) VALUES "
        "('marenostone', 'brown granite', 'Brown Granite', 'Chocolate Classic', 'Granite', 't'),"
        "('polonine', 'artemis', 'ARTEMIS', 'Andes', 'Quartzite', 't');"
        "INSERT INTO origin_decision (source, variant_norm, stone_type_norm, variant_display, country_iso, widen, decided_at) VALUES "
        "('zucchi', 'honey', 'onyx', 'Honey', 'IR', 0, 't'),"           # the global mint's origin, as decide() wrote it
        "('polonine', 'andes', 'quartzite', 'Andes', 'BR', 1, 't'),"
        "('varsha', 'black cosmic', 'granite', 'Black Cosmic', 'IN', 1, 't');")
    conn.commit(); conn.close()


def _legacy_projection(path) -> Decisions:
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    actions = {(r["source"], r["variant_norm"]): {"action": r["action"], "seed_color": r["seed_color"],
                                                   "seed_type": r["seed_type"], "seed_country": r["seed_country"],
                                                   "seed_name": r["seed_name"], "spelling": r["variant_display"],
                                                   "source": r["source"], "asked_by": r["asked_by"]}
               for r in conn.execute("SELECT * FROM variety_decision")}
    scoped = {(r["source"], r["variant_norm"]): (r["alias_of"], r["seed_type"] or "")
              for r in conn.execute("SELECT * FROM scoped_alias")}
    origins = {(r["source"], r["variant_norm"], r["stone_type_norm"]): r["country_iso"]
               for r in conn.execute("SELECT * FROM origin_decision")}
    widen = {(r["source"], r["variant_norm"], r["stone_type_norm"]): r["country_iso"]
             for r in conn.execute("SELECT * FROM origin_decision WHERE widen = 1")}
    conn.close()
    return Decisions.from_legacy(actions, scoped, origins, widen)


# what existed when those legacy rows were written: Andes exists, Chocolate Classic and Honey do not yet
def _exists(name, stone_type):
    return (_norm(name), _norm(stone_type)) in {("andes", "quartzite"), ("black cosmic", "granite")}


def _alias(name, stone_type):
    return None


def test_legacy_rows_become_statements_once_and_the_two_projections_agree(tmp_path, monkeypatch):
    db = tmp_path / "config.db"
    monkeypatch.setattr(store, "config_db_path", lambda: db)
    _legacy_db(db)
    legacy = _legacy_projection(db)
    store.open_store().close()                                    # the migration runs on this open
    migrated = decisions_store.load_decisions(exists_as=_exists, alias_target=_alias)
    assert migrated.statements == legacy.statements
    assert migrated.vendor_origins == legacy.vendor_origins
    assert migrated.widened == legacy.widened
    # the origin-queue confirmation with no statement became "varsha's Black Cosmic is Black Cosmic, from IN":
    # the one designed difference from the legacy projection, a binding to itself (the legacy origin row
    # carried only the normalized type, so that is what the statement can carry)
    self_binding = {("varsha", "black cosmic"): ("Black Cosmic", "granite")}
    assert migrated.bindings == {**legacy.bindings, **self_binding}
    rows = {(r["source"], r["spelling_norm"]): r for r in decisions_store._statement_rows()}
    assert rows[("varsha", "black cosmic")]["origin_iso"] == "IN" and rows[("varsha", "black cosmic")]["widen"] == 1
    # applied once: a second open changes nothing
    before = decisions_store._statement_rows()
    store.open_store().close()
    assert decisions_store._statement_rows() == before
    assert store.migration_applied(store.STATEMENTS_MIGRATION)


def test_a_fresh_store_marks_the_migration_with_nothing_to_move(tmp_path, monkeypatch):
    db = tmp_path / "config.db"
    monkeypatch.setattr(store, "config_db_path", lambda: db)
    store.open_store().close()
    assert store.migration_applied(store.STATEMENTS_MIGRATION)
    assert decisions_store._statement_rows() == []
