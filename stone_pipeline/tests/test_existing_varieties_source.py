"""The matcher and curate read the SAME existing-variety file, chosen in ONE place (`existing_varieties_file`):
the live Medusa export when it exists, else the committed Id-free base, else a hard error.

The trap: a factory reset deletes the live export and Blokport re-exports only after the next pull. The matcher
already fell back to the base, but curate silently loaded an EMPTY existing index for the same produce: every
alias or collision verdict degraded to a fuzzy or mint card, so confirming those cards minted duplicates of
varieties that already existed. Same input, two answers. Now one file, and a missing file stops the run.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

from stone_pipeline.aliases.surfaces import Stores
import pytest

from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate

KEY = "granite__alpine_00000000-0000-0000-0000-000000000001"


def _write(path: Path, name: str, with_id: bool) -> None:
    fields = (["Id"] if with_id else []) + ["Key", "Name", "Type", "Aliases"]
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerow({**({"Id": "var_1"} if with_id else {}), "Key": KEY, "Name": name, "Type": "Granite", "Aliases": "Alpine Granite"})


def _paths(tmp_path: Path):
    # the real paths, with only the two existing-variety files redirected. dataclasses.replace keeps the derived
    # properties (export_file, synonyms_dir, ...) that a copy of vars() would drop: a callee reading one then
    # failed whenever its cache was cold, i.e. when this file ran alone
    return dataclasses.replace(loaders.SETTINGS.paths, variants_export_csv=tmp_path / "variants_export.csv",
                               variants_export_base_csv=tmp_path / "variants_export_base.csv")


def _settings(paths):
    return dataclasses.replace(loaders.SETTINGS, paths=paths)


def test_live_export_wins_when_present(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write(paths.export_file, "Alpine Live", with_id=True)
    _write(paths.variants_export_base_csv, "Alpine Base", with_id=False)
    monkeypatch.setattr(loaders, "SETTINGS", _settings(paths))
    assert loaders.existing_varieties_file() == paths.export_file
    assert curate.load_existing().by_name_type[("alpine live", "granite")]["Key"] == KEY


def test_base_serves_both_matcher_and_curate_when_the_export_is_absent(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write(paths.variants_export_base_csv, "Alpine", with_id=False)
    monkeypatch.setattr(loaders, "SETTINGS", _settings(paths))
    assert loaders.existing_varieties_file() == paths.variants_export_base_csv
    # curate's existing index is populated from the base; a variety's aliases come from the alias stores (P2-10
    # C3/C4), never from the file's Aliases column, so the seed's alias resolves to its owner
    from stone_pipeline.aliases import stores as alias_stores
    monkeypatch.setattr(alias_stores, "scraper_stores",
                        lambda backbone=None: Stores(seed={KEY: (["Alpine Granite"], [])}))
    imp = curate.load_existing()
    assert imp.by_name_type[("alpine", "granite")]["Aliases"] == "Alpine Granite"
    # the matcher's table reads the same file
    table = loaders.load_variants(loaders.existing_varieties_file(), Stores())
    assert [v.key for v in table.by_id.values()] == [KEY]


def test_neither_file_is_a_hard_error(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders, "SETTINGS", _settings(_paths(tmp_path)))
    with pytest.raises(FileNotFoundError, match="variants_export.csv"):
        loaders.existing_varieties_file()
    with pytest.raises(FileNotFoundError, match="variants_export_base.csv"):
        curate.load_existing()
