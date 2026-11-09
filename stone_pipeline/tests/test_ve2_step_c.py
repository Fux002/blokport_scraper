"""VE2 step C: once BlokPort's prod release publishes the columns, Medusa's attributes.csv must carry status and
internal, and its variants export must carry status. A file without them fails loud, naming the file, instead of
reading every value as active (which would let new data map onto archived or internal values). The scraper's own
Id-free base and delta never carry status, by design, and stay readable."""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config import settings
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import variants as variants_loader

ATTRS_OLD = "category,value,sourceid\ncolor,White,c1\n"
ATTRS_NEW = "category,value,sourceid,status,internal\ncolor,White,c1,active,false\ncolor,Grey,c2,archived,false\n"
EXPORT_OLD = "Id,Key,Name,Image,Aliases\nv1,slab_marble_a_1,A,,\n"
EXPORT_NEW = "Id,Key,Name,Image,Aliases,status\nv1,slab_marble_a_1,A,,,active\nv2,slab_marble_b_2,B,,,archived\n"
BASE = "Key,Name,Image,Aliases,Volume per kg (m³/kg)\nslab_marble_a_1,A,,,\n"


def test_both_status_columns_are_required():
    assert settings.ATTRIBUTES_STATUS_REQUIRED is True
    assert settings.VARIANTS_EXPORT_STATUS_REQUIRED is True


def test_an_attributes_file_without_status_fails_loud_naming_the_file(tmp_path):
    path = tmp_path / "attributes.csv"
    path.write_text(ATTRS_OLD, encoding="utf-8")
    with pytest.raises(ValueError, match=f"{path}: missing required column"):
        loaders.load_attributes(path)


def test_an_attributes_file_with_status_parses(tmp_path):
    path = tmp_path / "attributes.csv"
    path.write_text(ATTRS_NEW, encoding="utf-8")
    assert loaders.restricted_ids(path) == {"c2"}


@pytest.fixture
def export_at(monkeypatch, tmp_path):
    """Medusa's live export is the file at SETTINGS.paths.variants_export_csv: point it at a tmp file."""
    path = tmp_path / "variants_export.csv"
    # the loaders read SETTINGS through their package (loaders.common.env), so it is replaced there
    monkeypatch.setattr(loaders, "SETTINGS", dataclasses.replace(
        loaders.SETTINGS, paths=dataclasses.replace(loaders.SETTINGS.paths, variants_export_csv=path)))
    return path


def test_medusas_export_without_status_fails_loud_naming_the_file(export_at):
    export_at.write_text(EXPORT_OLD, encoding="utf-8")
    with pytest.raises(ValueError, match=f"{export_at}: missing required column"):
        variants_loader._read_variant_records(export_at)


def test_medusas_export_with_status_parses(export_at):
    export_at.write_text(EXPORT_NEW, encoding="utf-8")
    records, _ = variants_loader._read_variant_records(export_at)
    assert [(r.key, r.archived) for r in records] == [("slab_marble_a_1", False), ("slab_marble_b_2", True)]


def test_the_scrapers_own_base_never_needs_status(export_at, tmp_path):
    base = tmp_path / "variants_export_base.csv"
    base.write_text(BASE, encoding="utf-8")
    records, _ = variants_loader._read_variant_records(base)
    assert [r.key for r in records] == ["slab_marble_a_1"]
