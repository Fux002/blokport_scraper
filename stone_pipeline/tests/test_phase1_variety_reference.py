"""The matcher's reference holds ONE record per variety (type, name), no longer one table per category split by
the Key's prefix. A variety has no category (D3): every category that shares the stone-variety vocabulary matches
against the same varieties, under the same Keys and ids. The (type, variant) identity stays atomic, the type from
the Key's type slug: one name under two types is two varieties. A Key of the old prefixed form is refused, never
split into a guessed category or type.
"""
from __future__ import annotations

import csv

import pytest

from stone_pipeline.reference import loaders
from stone_pipeline.phase1.variety_key import new_key

MARBLE_X, GRANITE_X = new_key("Marble", "Xanadu"), new_key("Granite", "Xanadu")


def _isolated(monkeypatch):
    monkeypatch.setattr(loaders, "_delete_keys", lambda: set())
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: set())
    monkeypatch.setattr(loaders, "pristine_seed_keys", lambda: frozenset())


def _base(path, rows):
    with path.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([["Key", "Name", "Type", "Aliases"], *rows])
    return path


def test_every_category_matches_against_the_same_varieties(tmp_path, monkeypatch):
    _isolated(monkeypatch)
    base = _base(tmp_path / "variants_export_base.csv", [[MARBLE_X, "Xanadu", "Marble", ""]])
    existing = loaders.load_existing_variants(base, ["slab", "block", "tile"])
    assert set(existing.tables) == {"slab", "block", "tile"}
    assert {k: list(t.by_id) for k, t in existing.tables.items()} == {c: [MARBLE_X] for c in ("slab", "block", "tile")}


def test_one_name_under_two_types_stays_two_varieties(tmp_path, monkeypatch):
    _isolated(monkeypatch)
    base = _base(tmp_path / "variants_export_base.csv",
                 [[MARBLE_X, "Xanadu", "Marble", ""], [GRANITE_X, "Xanadu", "Granite", ""]])
    table = loaders.load_variants(base)
    assert sorted(v.key for v in table.by_id.values()) == sorted([MARBLE_X, GRANITE_X])


def test_an_old_prefixed_key_is_refused(tmp_path, monkeypatch):
    _isolated(monkeypatch)
    base = _base(tmp_path / "variants_export_base.csv", [["slab_marble_xanadu_1a2b", "Xanadu", "Marble", ""]])
    with pytest.raises(ValueError, match="variety Key"):
        loaders.load_variants(base)


@pytest.mark.parametrize("name", ["Mgt", "Xanadu"])   # a bare-code name, and a retired row (below)
def test_a_prefixed_key_is_refused_on_every_row_even_one_the_matcher_skips(tmp_path, monkeypatch, name):
    _isolated(monkeypatch)
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: {"slab_marble_xanadu_1a2b"})
    base = _base(tmp_path / "variants_export_base.csv", [["slab_marble_xanadu_1a2b", name, "Marble", ""]])
    with pytest.raises(ValueError, match="variety Key"):
        loaders.load_variants(base)
