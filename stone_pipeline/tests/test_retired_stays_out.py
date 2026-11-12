"""A RETIRED variety stays out of every later produce (Mas's condition before prod's pass 2, 12 Nov): the candidate
line's twin of main's test, on its old branch-prefixed Keys.

Retiring a variety records its Keys in config.db's retired memory, but the lagging live Medusa export keeps listing
them until BlokPort's removed lane deletes them. Two filters keep them out meanwhile, each pinned here (the Volakas
-> Volocas shape: marble Volakas's three category copies retired, Volocas carries 'Volakas'):
  * emit: the retired-decision filter in emit_catalog.build, so the export's rows never reach 1_variants_full;
  * the matcher: the retired Keys join delete_keys in reference/loaders/variants._fill_tables, so a retired Key is no
    match surface and its name binds the variety that carries it as an alias.
Test-only: the prod pin's code is unchanged. Hermetic: tmp files and a monkeypatched retired memory. No em dashes.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.reference import loaders
from stone_pipeline.stages import decisions, emit_catalog

VOLOCAS = {"slab": "slab_marble_volocas_73dc1717-fbf9-4ffb-959b-d79dcfc6a308",
           "block": "block_marble_volocas_71d80563-82af-51b6-817a-b3056c3da817",
           "tile": "tile_marble_volocas_50bfaa91-e605-5f18-b77b-affc7c872cea"}
VOLAKAS = {"slab": "slab_marble_volakas_0a662110-822d-48bf-b173-44cb20ac532a",
           "block": "block_marble_volakas_d3e32216-8ffd-4307-adaa-a92fd5308fbc",
           "tile": "tile_marble_volakas_79ea08ad-2e0c-5174-a04e-cde3d2509dc2"}


def _write(path: Path, header, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([header, *rows])
    return path


def _base_rows(keys: dict[str, str], name: str, aliases: str):
    return [[keys[c], name, "", aliases, ""] for c in ("slab", "block", "tile")]


def _emit(tmp_path, monkeypatch, retired: set[str]) -> set[str]:
    """One build: the base holds Volocas's copies (carrying 'Volakas'); the live export still lists Volakas's copies."""
    export = _write(tmp_path / "variants_export.csv", ["Id", *emit_catalog._COLS],
                    [["01" + k[-6:], *row] for k, row in
                     zip([*VOLOCAS.values(), *VOLAKAS.values()],
                         _base_rows(VOLOCAS, "Volocas", "Volakas") + _base_rows(VOLAKAS, "Volakas", ""))])
    paths = dataclasses.replace(SETTINGS.paths, review_dir=tmp_path / "review", variants_export_csv=export,
                                curate_delta_csv=tmp_path / "no_delta.csv")
    monkeypatch.setattr(emit_catalog, "SETTINGS", dataclasses.replace(SETTINGS, paths=paths))
    monkeypatch.setattr(decisions, "load_retired", lambda: set(retired))
    base = _write(tmp_path / "variants_export_base.csv", emit_catalog._COLS, _base_rows(VOLOCAS, "Volocas", "Volakas"))
    out = tmp_path / "1_variants_full.csv"
    emit_catalog.build(base, image_keys=set(), out_path=out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        return {r["Key"] for r in csv.DictReader(h)}


def test_the_live_export_s_rows_reach_the_full_file_until_the_variety_is_retired(tmp_path, monkeypatch):
    assert set(VOLAKAS.values()) <= _emit(tmp_path, monkeypatch, retired=set())   # the export union adds them


def test_a_retired_key_in_the_live_export_never_reaches_the_full_file(tmp_path, monkeypatch):
    emitted = _emit(tmp_path, monkeypatch, retired=set(VOLAKAS.values()))
    assert emitted.isdisjoint(VOLAKAS.values()) and set(VOLOCAS.values()) <= emitted


@pytest.fixture
def reference(tmp_path, monkeypatch):
    """The matcher's slab table over a base holding both varieties' slab copies, as the lagging export still does."""
    monkeypatch.setattr(loaders, "_delete_keys", lambda: set())
    monkeypatch.setattr(loaders, "pristine_seed_keys", lambda: frozenset())
    base = _write(tmp_path / "variants_export_base.csv", emit_catalog._COLS,
                  [[VOLOCAS["slab"], "Volocas", "", "Volakas", ""], [VOLAKAS["slab"], "Volakas", "", "", ""]])

    def table(retired: set[str]):
        monkeypatch.setattr(decisions, "load_retired", lambda: set(retired))
        return loaders.load_variants(base, "slab", "slab_")
    return table


def _bind(table, spelling: str):
    index = build_variation_index(table, loaders.load_backbone())
    match = VariationEngine(index, auto_accept=90, review_floor=80).match(spelling, block_type="marble")
    return table.by_id[match.cid].key if match.cid else None


def test_a_retired_key_is_no_match_surface_and_its_name_binds_the_alias_owner(reference):
    table = reference({VOLAKAS["slab"]})
    assert VOLAKAS["slab"] not in {v.key for v in table.by_id.values()}
    assert _bind(table, "Volakas") == VOLOCAS["slab"]


def test_before_the_retire_the_name_binds_its_own_variety(reference):
    assert _bind(reference(set()), "Volakas") == VOLAKAS["slab"]
