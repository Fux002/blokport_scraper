"""Wave 4b: the existing-variety file is read once per consumer, and provenance names the file actually read.

load_all read the combined export once PER CATEGORY (three passes over ~36k rows, handles never closed) and
recorded the live export's hash even when the matcher had read the committed base; curate read the same file
once per category at two call sites (six passes per produce).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate


@pytest.fixture
def count_opens(monkeypatch):
    target = loaders.existing_varieties_file()
    loaders.pristine_seed_keys()          # its own (cached) read of the seed; not the matcher's read
    opens = {"n": 0}
    orig = Path.open

    def counting(self, *a, **k):
        if self == target:
            opens["n"] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(Path, "open", counting)
    return opens


def test_load_all_reads_the_existing_variety_file_once(count_opens):
    ref = loaders.load_all()
    assert count_opens["n"] == 1
    assert {b for b in ref.variants} >= {"slab", "block"}
    assert ref.variants["slab"].by_id and ref.variants["block"].by_id


def test_provenance_names_the_file_the_matcher_actually_read():
    ref = loaders.load_all()
    used = loaders.existing_varieties_file()
    assert ref.versions["variants_source"] == used.name
    assert ref.versions["variants_export"] == loaders.content_hash(used)


def test_curate_reads_the_existing_variety_file_once_for_every_branch(count_opens):
    imports = curate.load_all_existing()
    assert count_opens["n"] == 1
    assert set(imports) == set(curate.BRANCHES)
    assert imports["slab"].by_name_type and imports["block"].by_name_type
