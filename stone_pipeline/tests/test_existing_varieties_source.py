"""The matcher and curate read the SAME existing-variety file, chosen in ONE place (`existing_varieties_file`):
the live Medusa export when it exists, else the committed Id-free base, else a hard error.

The trap: a factory reset deletes the live export and Blokport re-exports only after the next pull. The matcher
already fell back to the base, but curate silently loaded an EMPTY existing index for the same produce: every
alias or collision verdict degraded to a fuzzy or mint card, so confirming those cards minted duplicates of
varieties that already existed. Same input, two answers. Now one file, and a missing file stops the run.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate

KEY = "slab_granite_alpine_00000000-0000-0000-0000-000000000001"


def _write(path: Path, name: str, with_id: bool) -> None:
    fields = (["Id"] if with_id else []) + ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerow({**({"Id": "var_1"} if with_id else {}), "Key": KEY, "Name": name, "Aliases": "Alpine Granite"})


def _paths(tmp_path: Path) -> SimpleNamespace:
    # the real paths (attributes etc. still resolve), with only the two existing-variety files redirected
    return SimpleNamespace(**{**vars(loaders.SETTINGS.paths),
                              "export_file": tmp_path / "variants_export.csv",
                              "variants_export_base_csv": tmp_path / "variants_export_base.csv"})


def test_live_export_wins_when_present(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write(paths.export_file, "Alpine Live", with_id=True)
    _write(paths.variants_export_base_csv, "Alpine Base", with_id=False)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    assert loaders.existing_varieties_file() == paths.export_file
    assert curate.load_existing("slab").by_name_type[("alpine live", "granite")]["Key"] == KEY


def test_base_serves_both_matcher_and_curate_when_the_export_is_absent(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write(paths.variants_export_base_csv, "Alpine", with_id=False)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    assert loaders.existing_varieties_file() == paths.variants_export_base_csv
    # curate's existing index is populated from the base: the alias surface resolves to its owner
    imp = curate.load_existing("slab")
    assert imp.by_name_type[("alpine", "granite")]["Aliases"] == "Alpine Granite"
    # the matcher's table reads the same file
    table = loaders.load_variants(loaders.existing_varieties_file(), "slab", key_prefix="slab")
    assert [v.key for v in table.by_id.values()] == [KEY]


def test_neither_file_is_a_hard_error(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=_paths(tmp_path)))
    with pytest.raises(FileNotFoundError, match="variants_export.csv"):
        loaders.existing_varieties_file()
    with pytest.raises(FileNotFoundError, match="variants_export_base.csv"):
        curate.load_existing("slab")
