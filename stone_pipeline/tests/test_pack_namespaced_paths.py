"""Per-material data paths follow ONE rule: the stone pack keeps today's flat paths, any other pack reads only its own
<base>/<pack>/ and never stone's (one material per deployment; DOMAIN_PACK.md)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import REPO_ROOT, SETTINGS, WORKSPACE_ROOT

CATALOG_SOURCE = WORKSPACE_ROOT / "catalog_source"
SYNONYMS = REPO_ROOT / "reference" / "synonyms"


@pytest.fixture
def pack(monkeypatch):
    def _activate(name: str) -> None:
        monkeypatch.setattr(settings, "active_pack", lambda: SimpleNamespace(name=name))
    return _activate


def test_stone_keeps_its_flat_paths(pack):
    pack("stone")
    assert SETTINGS.paths.synonyms_dir == SYNONYMS
    assert SETTINGS.paths.backbone_additions_dir == CATALOG_SOURCE / "backbone_additions"
    assert SETTINGS.paths.origin_map_csv == CATALOG_SOURCE / "origin_map.csv"


def test_another_pack_reads_only_its_own(pack):
    pack("wood")
    assert SETTINGS.paths.synonyms_dir == SYNONYMS / "wood"
    assert SETTINGS.paths.backbone_additions_dir == CATALOG_SOURCE / "wood" / "backbone_additions"
    assert SETTINGS.paths.origin_map_csv == CATALOG_SOURCE / "wood" / "origin_map.csv"
