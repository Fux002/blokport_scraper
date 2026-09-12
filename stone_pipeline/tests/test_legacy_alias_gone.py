"""Wave 4c: one alias mechanism. The legacy `action='alias'` decision has had no writer since the review
statement (decide -> scoped_alias) replaced it; the store, the readers and curate's operator-alias arm kept
serving a row nothing can create. The action is gone: the store refuses it, and a database that still holds
such rows refuses to open, naming them (re-decide via /review/decide), rather than silently ignoring an
operator decision.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from stone_pipeline.config import decisions_store, store


def test_the_alias_action_is_refused_by_the_store():
    with pytest.raises(decisions_store.InvalidDecision, match="mint.*reject"):
        decisions_store.set_variety_decision("Bianco Spelling", "alias")
    assert not hasattr(decisions_store, "alias_map") and not hasattr(decisions_store, "alias_type_map")


def test_a_database_with_legacy_alias_rows_refuses_to_open_naming_them(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(store.open_store(path)) as conn:
        conn.execute("INSERT INTO variety_decision (source, variant_norm, variant_display, action, alias_of, decided_at) "
                     "VALUES ('', 'black turtle', 'Black Turtle', 'alias', 'Black Sea', '2026-01-01T00:00:00Z')")
        conn.commit()
    with closing(sqlite3.connect(str(path))) as raw:
        raw.execute("PRAGMA user_version = 2")                  # from before the alias action was removed
    with pytest.raises(RuntimeError, match="Black Turtle"):
        store.open_store(path)
