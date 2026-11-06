"""The one reader of each Medusa download, from_medusa/<env>/attributes.csv and variants_export.csv (VE2 design,
section 1): parse by header name, a missing required column or a bad status/internal cell fails loud, the
blank-row rules (and for attributes the conflicting-duplicate guard) live here once, and no other module parses
the files."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from stone_pipeline.reference.medusa_files import (AttributeValue, ExportVariation, append_attributes,
                                                   read_attributes, read_variants_export)

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


# A module parses attributes.csv when it reads a CSV (the csv module, polars or pandas) and takes the file's columns
# from its rows: the sourceid, or the category and value pair (by .get, subscript or pl.col). Only medusa_files.py
# may (ledger/render.py writes the shape, never reads it).
_CSV_READER = re.compile(r"csv\.(Dict)?[rR]eader|\b(pl|polars|pd|pandas)\.(read|scan)_csv")
_ATTRIBUTE_COLUMNS = (re.compile(r"""["']sourceid["']"""),
                      re.compile(r"""(get\(|\[|col\()["']category["'][\s\S]*(get\(|\[|col\()["']value["']"""))


def _parses_attributes(text: str) -> bool:
    return bool(_CSV_READER.search(text)) and any(p.search(text) for p in _ATTRIBUTE_COLUMNS)


@pytest.mark.parametrize("snippet", [
    'for r in csv.DictReader(h): ids[r.get("category"), r.get("value")] = r.get("sourceid")',
    'for r in csv.DictReader(h): out[r["category"]] = r["value"]',
    'df = pl.read_csv(path); df.filter(pl.col("category") == "finish").select(pl.col("value"))',
    'df = pd.read_csv(path); ids = df["sourceid"]',
])
def test_the_guard_recognises_an_attributes_parser(snippet):
    assert _parses_attributes(snippet)


def test_only_medusa_files_parses_the_attributes_file():
    offenders = []
    for root in ("stone_pipeline", "scrapers", "deploy", "image_pipeline"):
        for path in sorted((WORKSPACE / root).rglob("*.py")):
            rel = path.relative_to(WORKSPACE).as_posix()
            if "/tests/" in rel or rel == "stone_pipeline/reference/medusa_files.py":
                continue
            if _parses_attributes(path.read_text(encoding="utf-8")):
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


# --- variants_export.csv ---------------------------------------------------------------------------------------

_EXPORT = "Id,Key,Name,Image,Aliases,Volume per kg (m³/kg)\n"


def _export(tmp_path, text: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / "variants_export.csv"
    p.write_text(text, encoding=encoding)
    return p


def test_export_old_shape_reads_every_row_active(tmp_path):
    p = _export(tmp_path, _EXPORT + "v1,slab_marble_a_1,A,https://x/a.png,A1|A2,0.0004\n")
    assert read_variants_export(p, require_status=False) == [
        ExportVariation("slab_marble_a_1", "A", "v1", "https://x/a.png", ("A1", "A2"), "0.0004", "active")]


def test_export_new_shape_carries_status(tmp_path):
    p = _export(tmp_path, "Id,Key,Name,Image,Aliases,status\nv1,slab_a_1,A,,,active\nv2,slab_b_1,B,,,Archived\n")
    got = read_variants_export(p, require_status=True)
    assert [(v.key, v.status, v.archived) for v in got] == [("slab_a_1", "active", False), ("slab_b_1", "archived", True)]


def test_export_reordered_and_extra_columns_parse_by_header_name(tmp_path):
    p = _export(tmp_path, " status ,Extra,Aliases, Name ,Key,Id\nactive,x, a | |b ,A,slab_a_1,v1\n")
    assert read_variants_export(p, require_status=True) == [
        ExportVariation("slab_a_1", "A", "v1", "", ("a", "b"), "", "active")]


def test_export_optional_columns_read_empty_when_absent(tmp_path):
    # the Id-free base shape, and an export without Image/Aliases: every reader tolerated both before
    base = _export(tmp_path, "Key,Name\nslab_a_1,A\n")
    assert read_variants_export(base, require_status=False) == [
        ExportVariation("slab_a_1", "A", "", "", (), "", "active")]


@pytest.mark.parametrize("header", ["Id,Key,Image,Aliases", "Id,Name,Image,Aliases", ""])
def test_export_missing_required_column_raises_naming_file_and_column(tmp_path, header):
    p = _export(tmp_path, header + "\n" if header else "")
    with pytest.raises(ValueError, match=r"variants_export\.csv: missing required column"):
        read_variants_export(p, require_status=False)


@pytest.mark.parametrize("status", ["", "deleted"])
def test_export_empty_or_invalid_status_cell_raises_naming_the_row(tmp_path, status):
    p = _export(tmp_path, f"Id,Key,Name,status\nv1,slab_a_1,A,active\nv2,slab_b_1,B,{status}\n")
    with pytest.raises(ValueError, match="line 3: status"):
        read_variants_export(p, require_status=False)


def test_export_require_status_without_the_column_raises(tmp_path):
    p = _export(tmp_path, _EXPORT)
    with pytest.raises(ValueError, match="missing required column.*status"):
        read_variants_export(p, require_status=True)


def test_export_blank_rows_skipped_and_half_rows_kept(tmp_path):
    p = _export(tmp_path, _EXPORT + ",,,,,\n\n,slab_a_1,A,,,\nv2,,B,,,\n")
    assert [(v.id, v.key, v.name) for v in read_variants_export(p, require_status=False)] == [
        ("", "slab_a_1", "A"), ("v2", "", "B")]


def test_export_bom_and_text_form(tmp_path):
    body = _EXPORT + "v1,slab_a_1,A,,,\r\n"
    from_file = read_variants_export(_export(tmp_path, body, encoding="utf-8-sig"), require_status=False)
    assert from_file == read_variants_export("\ufeff" + body, require_status=False)
    assert [v.key for v in from_file] == ["slab_a_1"]


def test_own_variants_keeps_the_status_column_whole(tmp_path):
    from deploy.fetch_inputs import own_variants
    p = _export(tmp_path, "Id,Key,Name,status\nv1,slab_a_1,A,archived\nv9,np-cattle-1,Cow,active\n")
    assert own_variants(p) == 1
    assert p.read_text(encoding="utf-8") == "Id,Key,Name,status\nv1,slab_a_1,A,archived\n"
    assert read_variants_export(p, require_status=True)[0].archived


def test_variety_names_reads_the_text_form():
    from stone_pipeline.standardise.tile_plan import variety_names
    assert variety_names("Id,Key,Name\nv1,slab_a_1,A\n,slab_b_1,B\nv3,slab_c_1,\n") == {"v1": "A"}


# Medusa's variants export reaches the code through these names; a function that opens one of them (directly or
# through a local bound to it) parses the export outside read_variants_export.
_EXPORT_MARKERS = ("export_file", "variants_export_csv", "existing_varieties_file")
_OPENERS = {"open", "read_text", "read_bytes"}


def _opens_the_export(func: ast.AST) -> bool:
    def marked(node) -> bool:
        text = ast.unparse(node)
        return any(m in text for m in _EXPORT_MARKERS) or (isinstance(node, ast.Name) and node.id in bound)
    bound: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None and marked(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.For) and marked(node.iter):
            bound |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in _OPENERS and marked(f.value):
            return True
        if isinstance(f, ast.Name) and f.id == "open" and node.args and marked(node.args[0]):
            return True
    return False


def test_only_medusa_files_parses_the_variants_export():
    offenders = []
    for root in ("stone_pipeline", "scrapers", "deploy", "image_pipeline"):
        for path in sorted((WORKSPACE / root).rglob("*.py")):
            rel = path.relative_to(WORKSPACE).as_posix()
            if "/tests/" in rel or rel == "stone_pipeline/reference/medusa_files.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders += [f"{rel}:{fn.name}" for fn in ast.walk(tree)
                          if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _opens_the_export(fn)]
    assert offenders == [], f"read the variants export through reference.medusa_files.read_variants_export: {offenders}"
