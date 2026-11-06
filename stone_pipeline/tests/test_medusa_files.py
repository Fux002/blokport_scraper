"""The one reader of from_medusa/<env>/attributes.csv (VE2 design, section 1): parse by header name, a missing
required column or a bad status/internal cell fails loud, the blank-row rules and the conflicting-duplicate guard
live here once, and no other module parses the file."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stone_pipeline.reference.medusa_files import AttributeValue, append_attributes, read_attributes

PACKAGE = Path(__file__).resolve().parents[1]
WORKSPACE = PACKAGE.parent


def _file(tmp_path, text: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / "attributes.csv"
    p.write_text(text, encoding=encoding)
    return p


def test_old_shape_reads_every_value_active_and_not_internal(tmp_path):
    p = _file(tmp_path, "category,value,sourceid\ncolor,Black,col_1\ncategory,Slabs,pcat_s\n")
    assert read_attributes(p, require_status=False) == [
        AttributeValue("color", "Black", "col_1", "active", False),
        AttributeValue("category", "Slabs", "pcat_s", "active", False)]


def test_new_shape_carries_status_and_internal(tmp_path):
    p = _file(tmp_path, "category,value,sourceid,status,internal\n"
                        "color,Black,col_1,active,false\n"
                        "color,Grey,col_2,ARCHIVED,False\n"
                        "color,Test,col_3,Active,TRUE\n")
    got = read_attributes(p, require_status=True)
    assert [(a.value, a.status, a.internal, a.assignable) for a in got] == [
        ("Black", "active", False, True), ("Grey", "archived", False, False), ("Test", "active", True, False)]


def test_reordered_and_extra_columns_parse_by_header_name(tmp_path):
    p = _file(tmp_path, " internal ,extra, sourceid ,status,value,category\nfalse,x,col_1,active,Black,color\n")
    assert read_attributes(p, require_status=True) == [AttributeValue("color", "Black", "col_1", "active", False)]


@pytest.mark.parametrize("header", ["category,value", "value,sourceid", "category,sourceid,status,internal", ""])
def test_missing_required_column_raises_naming_file_and_columns(tmp_path, header):
    p = _file(tmp_path, header + "\n" if header else "")
    with pytest.raises(ValueError, match=r"attributes\.csv: missing required column"):
        read_attributes(p, require_status=False)


def test_missing_column_message_names_each_missing_column(tmp_path):
    p = _file(tmp_path, "category\ncolor\n")
    with pytest.raises(ValueError, match="value, sourceid"):
        read_attributes(p, require_status=False)


@pytest.mark.parametrize("status,internal,bad", [
    ("", "false", "status"), ("retired", "false", "status"),
    ("active", "", "internal"), ("active", "yes", "internal")])
def test_empty_or_invalid_status_or_internal_cell_raises_naming_the_row(tmp_path, status, internal, bad):
    p = _file(tmp_path, "category,value,sourceid,status,internal\n"
                        "color,Black,col_1,active,false\n"
                        f"color,Grey,col_2,{status},{internal}\n")
    with pytest.raises(ValueError, match=rf"line 3: {bad}"):
        read_attributes(p, require_status=False)


@pytest.mark.parametrize("header", ["category,value,sourceid", "category,value,sourceid,status",
                                    "category,value,sourceid,internal"])
def test_require_status_without_the_columns_raises(tmp_path, header):
    p = _file(tmp_path, header + "\n")
    with pytest.raises(ValueError, match="missing required column"):
        read_attributes(p, require_status=True)


def test_one_status_column_alone_is_read_with_the_other_defaulted(tmp_path):
    p = _file(tmp_path, "category,value,sourceid,status\ncolor,Grey,col_2,archived\n")
    assert read_attributes(p, require_status=False) == [AttributeValue("color", "Grey", "col_2", "archived", False)]


def test_bom_is_dropped_from_a_file_and_from_text(tmp_path):
    body = "category,value,sourceid\ncolor,Black,col_1\n"
    expected = [AttributeValue("color", "Black", "col_1", "active", False)]
    assert read_attributes(_file(tmp_path, body, encoding="utf-8-sig"), require_status=False) == expected
    assert read_attributes("﻿" + body, require_status=False) == expected


def test_text_and_path_forms_read_the_same(tmp_path):
    body = "category,value,sourceid\r\ncolor,Black,col_1\r\nfinish,Polished,fin_1\r\n"
    assert read_attributes(body, require_status=False) == read_attributes(_file(tmp_path, body), require_status=False)


def test_blank_rows_skipped_and_an_empty_sourceid_kept(tmp_path):
    p = _file(tmp_path, "category,value,sourceid\n"
                        "color,,col_1\n"          # no value: skipped
                        ",Black,col_2\n"          # no category: skipped
                        "\n"                      # blank line: skipped
                        "  color , Grey ,  \n"    # cells stripped; an empty id kept
                        "color,Black\n")          # a short row: sourceid empty, kept
    assert read_attributes(p, require_status=False) == [
        AttributeValue("color", "Grey", "", "active", False), AttributeValue("color", "Black", "", "active", False)]


def test_conflicting_duplicate_raises_across_statuses(tmp_path):
    p = _file(tmp_path, "category,value,sourceid,status,internal\n"
                        "color,Amber,id_a,archived,false\ncolor,AMBER,id_b,active,true\n")
    with pytest.raises(ValueError, match="unique per category"):
        read_attributes(p, require_status=True)


def test_same_id_repeat_and_id_less_repeat_are_allowed(tmp_path):
    p = _file(tmp_path, "category,value,sourceid\ncolor,Amber,id_a\ncolor,amber,id_a\ncolor,Amber,\n"
                        "finish,Amber,id_f\n")    # another category: a different name
    assert [a.sourceid for a in read_attributes(p, require_status=False)] == ["id_a", "id_a", "", "id_f"]


def test_append_follows_the_files_header_and_reads_back(tmp_path):
    p = _file(tmp_path, "sourceid,category,value,status,internal,note\ncol_1,color,Black,active,false,x\n")
    append_attributes(p, [AttributeValue("color", "Grey", "col_2", "active", False)])
    assert p.read_text(encoding="utf-8").splitlines()[-1] == "col_2,color,Grey,active,false,"
    assert [a.value for a in read_attributes(p, require_status=True)] == ["Black", "Grey"]


def test_append_to_the_old_shape_writes_three_columns(tmp_path):
    p = _file(tmp_path, "category,value,sourceid\ncolor,Black,col_1\n")
    append_attributes(p, [AttributeValue("color", "Grey", "col_2", "active", False)])
    # csv.writer's own line ending, exactly as the append it replaces wrote
    assert p.read_bytes() == b"category,value,sourceid\ncolor,Black,col_1\ncolor,Grey,col_2\r\n"


# A module parses attributes.csv when it reads a CSV and takes the file's columns from its rows: the sourceid, or
# the category and value pair. Only medusa_files.py may (ledger/render.py writes the shape, never reads it).
_CSV_READER = re.compile(r"csv\.(Dict)?[rR]eader")
_ATTRIBUTE_COLUMNS = (re.compile(r"""["']sourceid["']"""),
                      re.compile(r"""get\(["']category["'][\s\S]*get\(["']value["']"""))


def test_only_medusa_files_parses_the_attributes_file():
    offenders = []
    for root in ("stone_pipeline", "scrapers", "deploy", "image_pipeline"):
        for path in sorted((WORKSPACE / root).rglob("*.py")):
            rel = path.relative_to(WORKSPACE).as_posix()
            if "/tests/" in rel or rel == "stone_pipeline/reference/medusa_files.py":
                continue
            text = path.read_text(encoding="utf-8")
            if _CSV_READER.search(text) and any(p.search(text) for p in _ATTRIBUTE_COLUMNS):
                offenders.append(rel)
    assert offenders == [], f"parse attributes.csv through reference.medusa_files.read_attributes: {offenders}"


def test_adopt_attribute_ids_appends_only_new_values_in_the_files_shape(tmp_path):
    from stone_pipeline.stages.decisions import adopt_attribute_ids
    old = _file(tmp_path, "category,value,sourceid\ncolor,Black,col_1\n")
    filled = {("color", "black"): ("Black", "col_1"), ("color", "sea green"): ("Sea Green", "col_9")}
    assert adopt_attribute_ids(filled, attributes_csv=old) == 1
    assert old.read_bytes() == b"category,value,sourceid\ncolor,Black,col_1\ncolor,Sea Green,col_9\r\n"
    assert adopt_attribute_ids(filled, attributes_csv=old) == 0        # idempotent

    new = tmp_path / "new.csv"
    new.write_text("category,value,sourceid,status,internal\ncolor,Black,col_1,archived,false\n", encoding="utf-8")
    assert adopt_attribute_ids(filled, attributes_csv=new) == 1
    assert [(a.value, a.status) for a in read_attributes(new, require_status=True)] == [
        ("Black", "archived"), ("Sea Green", "active")]


def test_finish_ids_reads_the_text_form():
    from stone_pipeline.standardise.tile_plan import finish_ids
    text = "﻿category,value,sourceid\nfinish,Polished,fin_1\nfinish,Honed,fin_2\ncolor,Black,col_1\n"
    assert finish_ids(text) == {"fin_1", "fin_2"}
