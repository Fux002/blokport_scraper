"""SC-4 2b (plan SC-3-SC-4-PLAN.md section 2b, spec section 6.2 Base files): the three published variety files
(1_variants_full.csv, 1_variants_update.csv, variants_export_base.csv) and the curate delta share ONE column list, Key,
Name, Type, Aliases. Image and Volume are gone: a variety's icons are per (Key, category) pairs on S3 (spec section 4),
and the volume was never the scraper's. The type is the row's own canonical type NAME, the column the backend's new
export carries (Id,Key,Name,Aliases,status,Type,owner), never un-slugged from the Key.
"""

from __future__ import annotations

import pytest

from stone_pipeline.reference import medusa_files
from stone_pipeline.reference import sync_variants_base
from stone_pipeline.stages import emit_catalog
from stone_pipeline.stages.curate import output as curate_output

COLUMNS = ["Key", "Name", "Type", "Aliases"]
_KEY = "granite__absolute-black"
_EXPORT = ("Id,Key,Name,Aliases,status,Type,owner\n"
           f"var_01,{_KEY},Absolute Black,Nero Assoluto|Black Absolute,active,Granite,scraper\n")


def test_every_variety_file_shares_the_one_column_list():
    assert emit_catalog._COLS == COLUMNS
    assert sync_variants_base.COLS == COLUMNS
    assert curate_output._IMPORT_COLS == COLUMNS


def test_the_export_reader_reads_the_type_and_no_image_or_volume():
    (row,) = medusa_files.read_variants_export(_EXPORT, require_status=True)
    assert (row.key, row.name, row.id, row.type, row.aliases) == (
        _KEY, "Absolute Black", "var_01", "Granite", ("Nero Assoluto", "Black Absolute"))
    assert not hasattr(row, "image") and not hasattr(row, "volume")
    assert not hasattr(medusa_files, "VOLUME_COLUMN")


def test_a_variety_file_without_a_type_column_fails_loud():
    with pytest.raises(ValueError, match="Type"):
        medusa_files.read_variants_export("Key,Name,Aliases\nk,Absolute Black,\n", require_status=False)


def test_the_emitted_rows_carry_exactly_the_four_columns(tmp_path):
    path = tmp_path / "variants_export_base.csv"
    path.write_text("Key,Name,Type,Aliases\n" f"{_KEY},Absolute Black,Granite,Nero Assoluto\n", encoding="utf-8")
    assert emit_catalog._rows(path) == [
        {"Key": _KEY, "Name": "Absolute Black", "Type": "Granite", "Aliases": "Nero Assoluto"}]


def test_the_base_sync_keeps_the_four_columns(tmp_path):
    full, base = tmp_path / "1_variants_full.csv", tmp_path / "variants_export_base.csv"
    full.write_text("Key,Name,Type,Aliases\n" f"{_KEY},Absolute Black,Granite,Nero Assoluto\n", encoding="utf-8")
    sync_variants_base.sync(full, base, publish=False)
    assert base.read_text(encoding="utf-8").splitlines() == [
        "Key,Name,Type,Aliases", f"{_KEY},Absolute Black,Granite,Nero Assoluto"]
