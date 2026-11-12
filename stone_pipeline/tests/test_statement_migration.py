"""A brand-new config.db marks the statement migration applied with nothing to move, and carries no legacy decision
table. The migration of a store from before the statement table (schema 4) is gone: since phase 1 a store at 1 to 11
is refused (test_phase1_store_versions), so no legacy store reaches the migration any more."""

from __future__ import annotations

import sqlite3

from stone_pipeline.config import decisions_store, store


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_a_fresh_store_marks_the_migration_with_nothing_to_move(tmp_path, monkeypatch):
    db = tmp_path / "config.db"
    monkeypatch.setattr(store, "config_db_path", lambda: db)
    store.open_store().close()
    assert store.migration_applied(store.STATEMENTS_MIGRATION)
    assert decisions_store._statement_rows() == []
    assert not _tables(db) & set(store._LEGACY_DECISION_TABLES)
