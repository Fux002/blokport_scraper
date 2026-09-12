"""Wave 4a: the config store's schema work runs once per database, and reading never creates one.

_migrate used to run its ~14 CREATE/ALTER statements and a repair DELETE on EVERY connection (the admin API
connects per request; decisions_store opens per call). And a read-only accessor on a host without config.db
(a produce on a fresh task) created an empty file that then shadowed the sources.yaml seed; eleven readers
carried their own exists() guard, the rest did not.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from stone_pipeline.config import decisions_store, store


def _statements(conn: sqlite3.Connection) -> list[str]:
    seen: list[str] = []
    conn.set_trace_callback(lambda sql: seen.append(sql.strip().upper()))
    return seen


def test_schema_work_runs_once_per_database(tmp_path, monkeypatch):
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    with closing(store.open_store()) as conn:                      # first open: creates + migrates
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    with closing(sqlite3.connect(str(path))) as raw:
        seen = _statements(raw)
        store._prepare(raw)                                        # every later connection
    ddl = [s for s in seen if s.startswith(("CREATE", "ALTER", "DELETE", "INSERT"))]
    assert ddl == [], f"schema work re-ran on a migrated database: {ddl[:3]}"


def test_an_older_database_is_migrated_then_stamped(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(str(path))) as raw:
        raw.executescript(store._SCHEMA)                           # a database from before the stamp
        raw.execute("PRAGMA user_version = 1")
    with closing(store.open_store(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"variety_decision", "origin_decision", "scoped_alias", "review_pending"} <= tables


@pytest.mark.parametrize("reader,empty", [
    (lambda: decisions_store.origin_decisions(), {}),
    (lambda: decisions_store.protected_keys(), set()),
    (lambda: decisions_store.list_pending("variety"), []),
    (lambda: decisions_store.variety_actions(), {}),
    (lambda: decisions_store.attribute_ids(), {}),
    (lambda: decisions_store.get_variety_origin("x", "marble"), None),
])
def test_a_reader_on_an_absent_store_returns_empty_and_creates_nothing(tmp_path, monkeypatch, reader, empty):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    assert reader() == empty
    assert not path.exists(), "a read materialised config.db (it would shadow the sources.yaml seed)"


def test_a_writer_still_creates_the_store_and_readers_then_see_it(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    decisions_store.set_attribute_id("color", "Verdigris", "col_01")
    assert path.exists()
    assert decisions_store.attribute_ids() == {("color", "verdigris"): ("Verdigris", "col_01")}
