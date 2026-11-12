"""A RETIRED variety stays out of every later produce (Mas's condition before prod's pass 2, 12 Nov).

Retiring a variety (POST /config/v1/variations/<key>/retire) records its Key in config.db's retired memory, but the
lagging live Medusa export keeps listing the variety until BlokPort's removed lane deletes it. Two filters keep it
out meanwhile, each pinned here (the Volakas -> Volocas shape: marble Volakas retired, Volocas carries 'Volakas'):
  * emit: the retired-decision filter in emit_catalog.build, so the export's row never reaches 1_variants_full;
  * the matcher: the retired Keys join delete_keys in reference/loaders/variants._table, so the retired Key is no
    match surface and its name binds the variety that carries it as an alias.
Hermetic: tmp files and a monkeypatched retired memory. No em dashes.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference import loaders
from stone_pipeline.stages import decisions, emit_catalog

VOLOCAS, VOLAKAS = new_key("Marble", "Volocas"), new_key("Marble", "Volakas")


def _write(path: Path, header, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([header, *rows])
    return path


def _emit(tmp_path, monkeypatch, retired: set[str]) -> set[str]:
    """One build: the base holds Volocas (carrying 'Volakas'); the live export still lists Volakas; no delta."""
    export = _write(tmp_path / "variants_export.csv", ["Id", "Key", "Name", "Aliases", "Type"],
                    [["01VOLOCAS", VOLOCAS, "Volocas", "Volakas", "Marble"],
                     ["01VOLAKAS", VOLAKAS, "Volakas", "", "Marble"]])
    paths = dataclasses.replace(SETTINGS.paths, review_dir=tmp_path / "review", variants_export_csv=export,
                                curate_delta_csv=tmp_path / "no_delta.csv")
    monkeypatch.setattr(emit_catalog, "SETTINGS", dataclasses.replace(SETTINGS, paths=paths))
    monkeypatch.setattr(decisions, "load_retired", lambda: set(retired))
    base = _write(tmp_path / "variants_export_base.csv", emit_catalog._COLS,
                  [[VOLOCAS, "Volocas", "Marble", "Volakas"]])
    out = tmp_path / "1_variants_full.csv"
    emit_catalog.build(base, icons={}, out_path=out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        return {r["Key"] for r in csv.DictReader(h)}


def test_the_live_export_s_row_reaches_the_full_file_until_the_variety_is_retired(tmp_path, monkeypatch):
    assert _emit(tmp_path, monkeypatch, retired=set()) == {VOLOCAS, VOLAKAS}     # the export union adds it


def test_a_retired_key_in_the_live_export_never_reaches_the_full_file(tmp_path, monkeypatch):
    assert _emit(tmp_path, monkeypatch, retired={VOLAKAS}) == {VOLOCAS}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    """The matcher's variety table over a base holding both varieties, as the lagging export still does."""
    monkeypatch.setattr(loaders, "_delete_keys", lambda: set())
    monkeypatch.setattr(loaders, "pristine_seed_keys", lambda: frozenset())
    base = _write(tmp_path / "variants_export_base.csv", emit_catalog._COLS,
                  [[VOLOCAS, "Volocas", "Marble", "Volakas"], [VOLAKAS, "Volakas", "Marble", ""]])

    def table(retired: set[str]):
        monkeypatch.setattr(decisions, "load_retired", lambda: set(retired))
        return loaders.load_variants(base)
    return table


def _bind(table, spelling: str):
    index = build_variation_index(table, loaders.load_backbone(SETTINGS.paths.backbone_json))
    match = VariationEngine(index, auto_accept=90, review_floor=80).match(spelling, block_type="marble")
    return table.by_id[match.cid].key if match.cid else None


def test_a_retired_key_is_no_match_surface_and_its_name_binds_the_alias_owner(reference):
    table = reference({VOLAKAS})
    assert VOLAKAS not in {v.key for v in table.by_id.values()}
    assert _bind(table, "Volakas") == VOLOCAS


def test_before_the_retire_the_name_binds_its_own_variety(reference):
    assert _bind(reference(set()), "Volakas") == VOLAKAS
