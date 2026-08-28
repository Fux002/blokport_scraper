"""Alias write-back state (state/writeback.py): persistence, provenance, and the undo path.

Self-contained (tmp_path + monkeypatch, no pipeline data), so it runs in CI unlike the data-dependent
match tests. B1: a learned fuzzy/phonetic alias is persisted as an exact alias for the next run, which
made a wrong high-scoring guess permanent and invisible. It now carries its learning METHOD as provenance
and can be reversed with forget_alias -- while the matcher's (variation_id, alias) view is unchanged.
"""

from __future__ import annotations

from stone_pipeline.state import writeback as wb


def _path(tmp_path, monkeypatch):
    path = tmp_path / "alias_writeback.csv"
    monkeypatch.setattr(wb, "_writeback_path", lambda name: path)
    return path


def test_persists_and_is_idempotent(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    w = wb.WriteBack()
    w.add_alias("vid-1", "Some Scraped Spelling", method="fuzzy")
    w.add_alias("vid-1", "Some Scraped Spelling", method="fuzzy")   # dup ignored
    assert w.flush() == 1
    again = wb.WriteBack()
    again.add_alias("vid-1", "Some Scraped Spelling", method="fuzzy")
    assert again.flush() == 0                                        # nothing new -> file untouched
    assert wb.load_alias_writeback(path) == [("vid-1", "Some Scraped Spelling")]


def test_records_the_learning_method_as_provenance(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    w = wb.WriteBack()
    w.add_alias("vid-9", "Fuzzy Learned Name", method="fuzzy")
    assert w.flush() == 1
    assert wb.load_alias_writeback_records(path) == [("vid-9", "Fuzzy Learned Name", "fuzzy")]
    assert wb.load_alias_writeback(path) == [("vid-9", "Fuzzy Learned Name")]   # matcher view unchanged


def test_forget_alias_reverses_a_wrong_learn(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    w = wb.WriteBack()
    w.add_alias("vid-1", "Keep This", method="projection_compact")
    w.add_alias("vid-2", "Wrong Guess", method="fuzzy")
    w.flush()

    assert wb.forget_alias("vid-2", "wrong guess", path) is True      # case-insensitive on the alias
    assert wb.load_alias_writeback(path) == [("vid-1", "Keep This")]   # only the wrong one is gone
    assert wb.forget_alias("vid-2", "Wrong Guess", path) is False      # already gone -> no-op


def test_forget_on_absent_file_is_a_no_op(tmp_path, monkeypatch):
    path = _path(tmp_path, monkeypatch)
    assert wb.forget_alias("vid-x", "anything", path) is False
    assert not path.exists()


def test_flush_upgrades_a_legacy_two_column_file(tmp_path, monkeypatch):
    # a pre-existing 2-column file (variation_id, alias) still loads; a new learning rewrites it under the
    # current 3-column schema, method blank for the old rows, and the old row is not duplicated.
    path = _path(tmp_path, monkeypatch)
    path.write_text("variation_id,alias\nvid-old,Legacy Name\n", encoding="utf-8")
    assert wb.load_alias_writeback(path) == [("vid-old", "Legacy Name")]

    w = wb.WriteBack()
    w.add_alias("vid-old", "Legacy Name", method="fuzzy")   # already present -> not re-added
    w.add_alias("vid-new", "New Name", method="phonetic")
    assert w.flush() == 1
    assert wb.load_alias_writeback_records(path) == [
        ("vid-old", "Legacy Name", ""), ("vid-new", "New Name", "phonetic")]
