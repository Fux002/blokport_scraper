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

_EXPORT = "Id,Key,Name,Type,Aliases\n"


def _export(tmp_path, text: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / "variants_export.csv"
    p.write_text(text, encoding=encoding)
    return p


def test_export_without_a_status_column_reads_every_row_active(tmp_path):
    p = _export(tmp_path, _EXPORT + "v1,marble__a_1,A,Marble,A1|A2\n")
    assert read_variants_export(p, require_status=False) == [
        ExportVariation("marble__a_1", "A", "v1", "Marble", ("A1", "A2"), "active")]


def test_export_new_shape_carries_status(tmp_path):
    p = _export(tmp_path, "Id,Key,Name,Type,Aliases,status\nv1,a__1,A,Marble,,active\nv2,b__1,B,Marble,,Archived\n")
    got = read_variants_export(p, require_status=True)
    assert [(v.key, v.status, v.archived) for v in got] == [("a__1", "active", False), ("b__1", "archived", True)]


def test_export_reordered_and_extra_columns_parse_by_header_name(tmp_path):
    p = _export(tmp_path, " status ,Extra,Aliases, Type , Name ,Key,Id\nactive,x, a | |b ,Marble,A,a__1,v1\n")
    assert read_variants_export(p, require_status=True) == [
        ExportVariation("a__1", "A", "v1", "Marble", ("a", "b"), "active")]


def test_export_optional_columns_read_empty_when_absent(tmp_path):
    # the Id-free base shape, and an export without Aliases: Id and Aliases read empty
    base = _export(tmp_path, "Key,Name,Type\na__1,A,Marble\n")
    assert read_variants_export(base, require_status=False) == [
        ExportVariation("a__1", "A", "", "Marble", (), "active")]


@pytest.mark.parametrize("header", ["Id,Key,Type,Aliases", "Id,Name,Type,Aliases", "Id,Key,Name,Aliases", ""])
def test_export_missing_required_column_raises_naming_file_and_column(tmp_path, header):
    p = _export(tmp_path, header + "\n" if header else "")
    with pytest.raises(ValueError, match=r"variants_export\.csv: missing required column"):
        read_variants_export(p, require_status=False)


@pytest.mark.parametrize("status", ["", "deleted"])
def test_export_empty_or_invalid_status_cell_raises_naming_the_row(tmp_path, status):
    p = _export(tmp_path, f"Id,Key,Name,Type,status\nv1,a__1,A,Marble,active\nv2,b__1,B,Marble,{status}\n")
    with pytest.raises(ValueError, match="line 3: status"):
        read_variants_export(p, require_status=False)


def test_export_require_status_without_the_column_raises(tmp_path):
    p = _export(tmp_path, _EXPORT)
    with pytest.raises(ValueError, match="missing required column.*status"):
        read_variants_export(p, require_status=True)


def test_export_blank_rows_skipped_and_half_rows_kept(tmp_path):
    p = _export(tmp_path, _EXPORT + ",,,,\n\n,a__1,A,Marble,\nv2,,B,Marble,\n")
    assert [(v.id, v.key, v.name) for v in read_variants_export(p, require_status=False)] == [
        ("", "a__1", "A"), ("v2", "", "B")]


def test_export_bom_and_text_form(tmp_path):
    body = _EXPORT + "v1,a__1,A,Marble,\r\n"
    from_file = read_variants_export(_export(tmp_path, body, encoding="utf-8-sig"), require_status=False)
    assert from_file == read_variants_export("\ufeff" + body, require_status=False)
    assert [v.key for v in from_file] == ["a__1"]


def test_export_text_form_errors_name_the_file_it_came_from(tmp_path):
    with pytest.raises(ValueError, match=r"^variants_export\.csv: missing"):
        read_variants_export("Key\na__1\n", require_status=False)
    with pytest.raises(ValueError, match=r"^variants_export_base\.csv: missing"):
        read_variants_export("Key\na__1\n", require_status=False, name="variants_export_base.csv")


def test_the_loader_names_the_base_file_in_an_error(tmp_path):
    from stone_pipeline.reference.loaders import load_existing_variants
    base = tmp_path / "variants_export_base.csv"
    base.write_text("Key\na__1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(str(base))):
        load_existing_variants(base, ["slab"])


def test_own_variants_keeps_the_status_column_whole(tmp_path):
    from stone_pipeline.io.medusa_inputs import own_variants
    # the scraper's own rows are the export's owner = scraper rows (D5); filtering keeps every other column whole
    p = _export(tmp_path, "Id,Key,Name,Type,status,owner\nv1,marble__a_1,A,Marble,archived,scraper\n"
                          "v9,np-cattle-1,Cow,Cattle,active,\n")
    assert own_variants(p) == 1
    assert p.read_text(encoding="utf-8") == "Id,Key,Name,Type,status,owner\nv1,marble__a_1,A,Marble,archived,scraper\n"
    assert read_variants_export(p, require_status=True)[0].archived


def test_variety_names_reads_the_text_form():
    from stone_pipeline.standardise.tile_plan import variety_names
    assert variety_names("Id,Key,Name,Type\nv1,a__1,A,Marble\n,b__1,B,Marble\nv3,c__1,,Marble\n") == {"v1": "A"}


# Medusa's variants export reaches the code through these names. Inside a function, the export is a marker
# expression, a call that returns it (existing_varieties_file(), Path(<export>), _text(client, <export key>), or a
# package function whose return names a marker, like image_intake._variant_files) or a local bound to any of those. A
# function reads the export outside the one reader when it opens it (.open / .read_text / .read_bytes) or passes it to
# any callee that neither reads through read_variants_export (directly or via other package functions, worked out
# from the source) nor is listed as not reading it. pl.read_csv(<export>) or a helper taking the path is therefore
# flagged until it reads through the reader.
_EXPORT_MARKERS = ("export_file", "variants_export_csv", "existing_varieties_file", "VARIANTS_KEY")
_OPENERS = {"open", "read_text", "read_bytes"}
# given the export, these return it again: as a path, or as its text fetched from S3
_CARRIERS = {"Path", "_text"}
# take the path or S3 key without parsing it: a carrier, its text form, an error or a message, a record of which file
# was read (ImportFile.path), the status-column rule (compares the path)
_NOT_A_READ = _CARRIERS | {"str", "FileNotFoundError", "say", "ImportFile", "variants_status_required"}


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _functions(tree: ast.AST) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _reads_through(modules: dict) -> set:
    """(module, function) pairs that read through read_variants_export, closed over calls: a bare name resolves in
    its own module when defined there (else it is an import: any package function of that name), an attribute call
    (mod.fn) to any package function of that name."""
    calls = {(rel, fn.name): {(isinstance(c.func, ast.Name), _callee(c)) for c in ast.walk(fn)
                               if isinstance(c, ast.Call)}
             for rel, tree in modules.items() for fn in _functions(tree)}
    through = {k for k, cs in calls.items() if any(name == "read_variants_export" for _, name in cs)}
    while True:
        more = {k for k, cs in calls.items() if k not in through
                and any(_resolves_through(k[0], bare, name, calls, through) for bare, name in cs)}
        if not more:
            return through
        through |= more


def _resolves_through(rel: str, bare: bool, name: str, defined, through: set) -> bool:
    if name == "read_variants_export":
        return True
    if bare and (rel, name) in defined:
        return (rel, name) in through
    return any(n == name for _, n in through)


def _returns_export(modules: dict) -> set:
    """Names of package functions that return an export marker expression."""
    return {fn.name for tree in modules.values() for fn in _functions(tree)
            if any(isinstance(r, ast.Return) and r.value is not None
                   and any(m in ast.unparse(r.value) for m in _EXPORT_MARKERS) for r in ast.walk(fn))}


def _export_offences(rel: str, func: ast.AST, through: set, defined, sources: set) -> list:
    bound: set[str] = set()

    def marked(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in bound
        if isinstance(node, ast.Call):
            return (any(m in ast.unparse(node.func) for m in _EXPORT_MARKERS) or _callee(node) in sources
                    or (_callee(node) in _CARRIERS and any(marked(a) for a in node.args)))
        return any(m in ast.unparse(node) for m in _EXPORT_MARKERS)
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None and marked(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.For) and marked(node.iter):
            bound |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        reads = _resolves_through(rel, isinstance(node.func, ast.Name), name, defined, through)
        if isinstance(node.func, ast.Attribute) and name in _OPENERS and marked(node.func.value):
            out.append(f"{rel}:{func.name} opens it")
        elif not reads and name not in _NOT_A_READ and any(
                marked(a) for a in [*node.args, *(k.value for k in node.keywords)]):
            out.append(f"{rel}:{func.name} passes it to {name}")
    return out


def _package_modules() -> dict:
    modules = {}
    for root in ("stone_pipeline", "scrapers", "deploy", "image_pipeline"):
        for path in sorted((WORKSPACE / root).rglob("*.py")):
            rel = path.relative_to(WORKSPACE).as_posix()
            if "/tests/" not in rel and rel != "stone_pipeline/reference/medusa_files.py":
                modules[rel] = ast.parse(path.read_text(encoding="utf-8"))
    return modules


@pytest.mark.parametrize("snippet", [
    "def f():\n    with SETTINGS.paths.export_file.open() as h:\n        return list(csv.DictReader(h))\n",
    "def f():\n    exp = SETTINGS.paths.export_file\n    return pl.read_csv(exp)\n",
    "def f():\n    return _ids(SETTINGS.paths.variants_export_csv, 'Id')\n",
    "def f(path=None):\n    path = Path(path or SETTINGS.paths.variants_export_csv)\n    return path.read_text()\n",
    "def f():\n    return _helper(existing_varieties_file())\n",
    "def f(client):\n    return parse(_text(client, f'{ENV}/{C.VARIANTS_KEY}'))\n",
    "def f():\n    for p in _variant_files():\n        return p.read_bytes()\n",
])
def test_the_guard_recognises_an_export_read_outside_the_reader(snippet):
    assert _export_offences("x.py", _functions(ast.parse(snippet))[0], set(), set(), {"_variant_files"})


def test_the_guard_accepts_a_helper_that_reads_through_the_reader():
    tree = ast.parse("def _helper(p):\n    return read_variants_export(p, require_status=False)\n"
                     "def f():\n    return _helper(SETTINGS.paths.export_file)\n")
    through = _reads_through({"x.py": tree})
    assert _export_offences("x.py", _functions(tree)[1], through, {("x.py", "_helper"), ("x.py", "f")}, set()) == []


def test_only_medusa_files_parses_the_variants_export():
    modules = _package_modules()
    through = _reads_through(modules)
    defined = {(rel, fn.name) for rel, tree in modules.items() for fn in _functions(tree)}
    sources = _returns_export(modules)
    offenders = [o for rel, tree in modules.items() for fn in _functions(tree)
                 for o in _export_offences(rel, fn, through, defined, sources)]
    assert offenders == [], f"read the variants export through reference.medusa_files.read_variants_export: {offenders}"
