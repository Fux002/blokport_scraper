"""Per-material data paths follow ONE rule: the stone pack keeps today's flat paths, any other pack reads only its own
<base>/<pack>/ and never stone's (one material per deployment; DOMAIN_PACK.md). The two bases are catalog_source/ and
the package's reference/, so a pack's data lives in exactly two folders: catalog_source/<pack>/ and reference/<pack>/."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import REPO_ROOT, SETTINGS, WORKSPACE_ROOT

CATALOG_SOURCE = WORKSPACE_ROOT / "catalog_source"
REFERENCE = REPO_ROOT / "reference"


@pytest.fixture
def pack(monkeypatch):
    def _activate(name: str) -> None:
        monkeypatch.setattr(settings, "active_pack", lambda: SimpleNamespace(name=name))
    return _activate


def test_stone_keeps_its_flat_paths(pack):
    pack("stone")
    assert SETTINGS.paths.synonyms_dir == REFERENCE / "synonyms"
    assert SETTINGS.paths.type_density_csv == REFERENCE / "type_density.csv"
    assert SETTINGS.paths.standard_slab_area_csv == REFERENCE / "standard_slab_area.csv"
    assert SETTINGS.paths.backbone_additions_dir == CATALOG_SOURCE / "backbone_additions"
    assert SETTINGS.paths.origin_map_csv == CATALOG_SOURCE / "origin_map.csv"


def test_another_pack_reads_only_its_own(pack):
    pack("wood")
    assert SETTINGS.paths.synonyms_dir == REFERENCE / "wood" / "synonyms"
    assert SETTINGS.paths.type_density_csv == REFERENCE / "wood" / "type_density.csv"
    assert SETTINGS.paths.standard_slab_area_csv == REFERENCE / "wood" / "standard_slab_area.csv"
    assert SETTINGS.paths.backbone_additions_dir == CATALOG_SOURCE / "wood" / "backbone_additions"
    assert SETTINGS.paths.origin_map_csv == CATALOG_SOURCE / "wood" / "origin_map.csv"


def test_stone_ships_its_reference_data_where_it_is_read(pack):
    # the committed stone files sit at the flat paths the stone pack reads, so the density and slab-area ladders
    # never silently fall through to the pack defaults
    pack("stone")
    assert SETTINGS.paths.type_density_csv.is_file()
    assert SETTINGS.paths.standard_slab_area_csv.is_file()
    assert SETTINGS.paths.synonyms_dir.is_dir()


@pytest.mark.parametrize("name, folder", [("stone", ""), ("wood", "wood")])
def test_the_additions_follow_a_redirected_catalog_source(pack, tmp_path, name, folder):
    # a Paths pointed at another catalog_source (a test's tmp, a copied workspace) writes its deltas there, never
    # into the committed catalog_source/
    pack(name)
    paths = dataclasses.replace(SETTINGS.paths, catalog_source_dir=tmp_path)
    assert paths.backbone_additions_dir == tmp_path / folder / "backbone_additions"
