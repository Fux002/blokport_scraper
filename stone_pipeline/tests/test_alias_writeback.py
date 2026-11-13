"""The run's learned guesses (state/writeback.py) become learned-alias PROPOSALS (P2-10 C2), and the old
alias_writeback.csv stays readable (aliases.migrate_learned) for the phase 1 store migration and the one-time move.

Self-contained (tmp stores, no pipeline data). B1 still holds: a learned guess carries its learning METHOD (and now
its vendor) as provenance; it is a proposal, so a wrong high-scoring guess never binds until a review approves it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from stone_pipeline.aliases import migrate_learned
from stone_pipeline.state import writeback as wb


def _rows(path):
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute("SELECT identity_key, value, method, source, status, score FROM learned_alias "
                            "ORDER BY identity_key, norm").fetchall()


def test_a_run_s_guesses_are_proposed_once_with_their_provenance(tmp_path, monkeypatch):
    from stone_pipeline.config import store
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    store.seed_from_yaml()                    # the config server's store: a proposal never creates it
    w = wb.WriteBack()
    w.add_alias("granite__a_1", "Some Scraped Spelling", method="fuzzy", source="zucchi", score=88.0)
    w.add_alias("granite__a_1", "some scraped spelling", method="phonetic", source="sentas")   # the run's dup
    assert w.flush() == 1
    again = wb.WriteBack()
    again.add_alias("granite__a_1", "Some Scraped Spelling", method="fuzzy", source="zucchi")
    assert again.flush() == 0                                       # a later run's repeat adds nothing
    assert _rows(path) == [("granite__a_1", "Some Scraped Spelling", "fuzzy", "zucchi", "proposed", 88.0)]


def test_an_empty_run_proposes_nothing_and_creates_no_store(tmp_path, monkeypatch):
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    assert wb.WriteBack().flush() == 0 and not path.exists()


def test_the_old_file_reads_as_its_records_and_absent_as_none(tmp_path):
    path = tmp_path / "alias_writeback.csv"
    assert migrate_learned.load_alias_writeback_records(path) == []
    path.write_text("variation_id,alias,method\nvid-9,Fuzzy Learned Name,fuzzy\n", encoding="utf-8")
    assert migrate_learned.load_alias_writeback_records(path) == [("vid-9", "Fuzzy Learned Name", "fuzzy")]


def test_a_legacy_two_column_file_reads_with_no_method(tmp_path):
    # its rows carry no learning method, so they are not provably guesses: the migration drops them (not_learned)
    path = tmp_path / "alias_writeback.csv"
    path.write_text("variation_id,alias\nvid-old,Legacy Name\n", encoding="utf-8")
    assert migrate_learned.load_alias_writeback_records(path) == [("vid-old", "Legacy Name", "")]
