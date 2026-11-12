"""Table 577 (G9, BE-SCRAPER-INTERFACE section F): ONE rule for every scraper reader of a category, the EXACT handle
(D11, the pack's lowercase plural), never the label and never a case fold.

  * T1 (from_medusa/<env>/attributes.csv) carries a `handle` column, LAST: a category row holds its handle, every
    other row ''. An export without the header is refused, and so is a row that breaks the rule;
  * the category registry (config/categories) is the ONE reader of T1's category rows, keyed by exact handle: a
    renamed label changes nothing, a pack category whose handle differs only in case ('Slabs') is refused by name,
    a handle that is no pack category is not ours;
  * every other reader (format_resolve, tree_build, validate's frozen check, restricted_values, the ledger's
    populate / render / dump / existing) takes a category from the registry, by handle or by pcat;
  * the ledger `attribute` table holds no category rows any more (seed and restore skip them); rows a live ledger
    already holds are inert: nothing reads them.
Hermetic: tmp files, a patched registry. No em dashes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stone_pipeline.config import categories, settings
from stone_pipeline.config.categories import CategoryHandleError
from stone_pipeline.reference.medusa_files import read_attributes

HEADER = "category,value,sourceid,status,internal,handle\n"
PACK_ROWS = ("category,Slabs,pcat_slab,active,false,slabs\n"
             "category,Blocks,pcat_block,active,false,blocks\n"
             "category,Tiles,pcat_tile,active,false,tiles\n")
REPO = Path(__file__).resolve().parents[2]


def _text(*rows: str) -> str:
    return HEADER + "".join(rows)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """The registry re-read from a tmp attributes.csv, restored after the test."""
    saved = (categories._ENV_PCATS, categories._BY_NAME, categories._BY_LABEL_CF)
    monkeypatch.setattr(settings, "_FROM_MEDUSA", tmp_path)
    monkeypatch.delenv("BLOKPORT_CAT_TILES_PCAT", raising=False)
    path = tmp_path / "attributes.csv"

    def load(text: str):
        path.write_text(text, encoding="utf-8")
        categories.refresh_category_pcats()
        return categories
    yield load
    categories._ENV_PCATS, categories._BY_NAME, categories._BY_LABEL_CF = saved


# --- T1: the handle column -------------------------------------------------------------------------------------------

def test_an_export_without_the_handle_header_is_refused():
    with pytest.raises(ValueError, match="handle"):
        read_attributes("category,value,sourceid,status,internal\ncategory,Slabs,pcat_slab,active,false\n",
                        require_status=True)


def test_a_category_row_carries_its_handle_and_every_other_row_none():
    values = read_attributes(_text("color,Black,col_1,active,false,\n", PACK_ROWS), require_status=True)
    assert [(a.category, a.value, a.handle) for a in values] == [
        ("color", "Black", ""), ("category", "Slabs", "slabs"), ("category", "Blocks", "blocks"),
        ("category", "Tiles", "tiles")]


@pytest.mark.parametrize("row", ["category,Slabs,pcat_slab,active,false,\n",      # a category without its handle
                                 "color,Black,col_1,active,false,black\n"])       # a handle on another kind
def test_a_row_breaking_the_handle_rule_is_refused_by_line(row):
    with pytest.raises(ValueError, match="line 2"):
        read_attributes(_text(row), require_status=True)


# --- the registry: the one reader of T1's category rows --------------------------------------------------------------

def test_the_registry_keys_every_category_by_its_exact_handle(registry):
    reg = registry(_text(PACK_ROWS))
    assert {c.name: c.pcat_id for c in reg.active_categories()} == {
        "slab": "pcat_slab", "block": "pcat_block", "tile": "pcat_tile"}


def test_a_renamed_label_changes_nothing(registry):
    reg = registry(_text("category,Slab Products,pcat_slab,active,false,slabs\n",
                         "category,Big Blocks,pcat_block,active,false,blocks\n",
                         "category,Tiles,pcat_tile,active,false,tiles\n"))
    assert reg.category_by_handle("slabs").pcat_id == "pcat_slab"
    assert reg.category_by_handle("blocks").pcat_id == "pcat_block"


def test_a_pack_handle_in_another_case_is_refused_by_name(registry):
    with pytest.raises(CategoryHandleError, match="'Slabs'"):
        registry(_text("category,Slabs,pcat_slab,active,false,Slabs\n"))


def test_a_handle_no_pack_category_has_is_not_ours(registry):
    reg = registry(_text(PACK_ROWS, "category,Sinks,pcat_sink,active,false,sinks\n"))
    assert {c.pcat_id for c in reg.active_categories()} == {"pcat_slab", "pcat_block", "pcat_tile"}


def test_a_category_is_found_by_its_exact_pcat(registry):
    reg = registry(_text(PACK_ROWS))
    assert reg.category_by_pcat("pcat_tile").handle == "tiles"
    assert reg.category_by_pcat("PCAT_TILE") is None and reg.category_by_pcat("") is None


def test_the_category_pcats_accessor_reads_exact_handles():
    values = read_attributes(_text(PACK_ROWS, "category,Sinks,pcat_sink,active,false,sinks\n"), require_status=True)
    assert categories.category_pcats(values) == {"slabs": "pcat_slab", "blocks": "pcat_block", "tiles": "pcat_tile"}


# --- the other readers take the registry -----------------------------------------------------------------------------

def test_format_resolve_takes_the_registry_pcat(registry):
    from stone_pipeline.stages.format_resolve import category_pcat_for_branch
    registry(_text("category,Slab Products,pcat_slab,active,false,slabs\n"))
    assert category_pcat_for_branch("slab") == "pcat_slab"
    assert category_pcat_for_branch("tile") is None                     # no pcat: inactive, never guessed


def test_restricted_values_key_a_category_by_its_handle(tmp_path):
    from stone_pipeline.reference.loaders import restricted_values
    path = tmp_path / "attributes.csv"
    path.write_text(_text("category,Tiles,pcat_tile,archived,false,tiles\n"), encoding="utf-8")
    assert restricted_values(path)["category"] == {"tiles"}


def test_value_frozen_holds_a_new_product_in_an_archived_category_by_handle(registry):
    from stone_pipeline.core.schema import CanonicalRow
    from stone_pipeline.stages import validate
    registry(_text(PACK_ROWS))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tile")
    validate.validate_row(row, frozen={"category": {"tiles"}}, acked={}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}
    held = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tile")
    validate.validate_row(held, frozen={"category": {"tiles"}}, acked={"ZUCCHI-1": {("category", "tiles")}},
                          source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in held.reject_reasons}


def test_the_ledger_carries_a_stored_handle_as_itself():
    from stone_pipeline.ledger.existing import _value
    assert _value("category", "tiles") == "tiles"
    with pytest.raises(ValueError, match="'Tiles'"):
        _value("category", "Tiles")


def test_a_fresh_ledger_with_no_category_rows_still_gives_every_product_its_handle(registry, tmp_path):
    from stone_pipeline.ledger.db import Ledger
    from stone_pipeline.ledger.populate import _category_handle_for
    registry(_text(PACK_ROWS))
    with Ledger.open(tmp_path / "dev.db", env="development") as lg:
        assert lg.execute("SELECT COUNT(*) FROM attribute").fetchone()[0] == 0
        assert [_category_handle_for(lg, p) for p in ("pcat_slab", "pcat_block", "pcat_tile", None)] == [
            "slabs", "blocks", "tiles", None]
        with pytest.raises(ValueError, match="pcat_unknown"):
            _category_handle_for(lg, "pcat_unknown")


def test_the_ledger_seed_and_restore_hold_no_category_rows(tmp_path):
    from stone_pipeline.ledger.bootstrap import restore_attribute_ids, seed_attributes
    from stone_pipeline.ledger.db import Ledger
    path = tmp_path / "attributes.csv"
    path.write_text(_text("color,Black,col_1,active,false,\n", PACK_ROWS), encoding="utf-8")
    with Ledger.open(tmp_path / "dev.db", env="development") as lg:
        assert seed_attributes(lg, path) == 1
        assert restore_attribute_ids(lg, path) == 1
        assert [tuple(r) for r in lg.execute("SELECT category, value FROM attribute")] == [("color", "Black")]


def test_the_dump_skips_a_live_ledgers_inert_category_rows_and_carries_the_handle_header(tmp_path, monkeypatch):
    from stone_pipeline.ledger import render
    from stone_pipeline.ledger.db import Ledger, now_iso
    now = now_iso()
    out = tmp_path / "dump.csv"
    with Ledger.open(tmp_path / "dev.db", env="development") as lg:
        for kind, value, mid in (("color", "Black", "col_1"), ("category", "Slabs", "pcat_old")):   # an old row
            lg.upsert("attribute", {"category": kind, "value": value, "medusa_id": mid, "state": "synced",
                                    "created_at": now, "updated_at": now}, pk=("category", "value"))
        render._dump_attributes(lg, out)
    assert [(a.category, a.value, a.handle) for a in read_attributes(out, require_status=True)] == [
        ("color", "Black", "")]


# --- guards: no reader keys a category by label, folds its case, or reads the ledger's category rows -------------------

_GUARDED = ("stone_pipeline/stages/tree_build.py", "stone_pipeline/stages/format_resolve.py",
            "stone_pipeline/stages/validate.py", "stone_pipeline/reference/loaders/attributes.py",
            "stone_pipeline/ledger/render.py", "stone_pipeline/ledger/populate.py",
            "stone_pipeline/ledger/existing.py", "stone_pipeline/ledger/bootstrap.py")


@pytest.mark.parametrize("path", _GUARDED)
def test_no_reader_resolves_a_category_by_label_or_by_a_case_fold(path):
    src = (REPO / path).read_text(encoding="utf-8")
    for pattern in (r"\battr\[\"category\"\]", r"match_key\(\s*(c\.plural|handle)\b", r"category_pcat\b",
                    r"_category_label\b", r"category_labels\b", r"\.label\b"):
        assert not re.search(pattern, src), f"{path} still matches {pattern!r}"


def test_nothing_reads_category_rows_from_the_ledger_attribute_table():
    hits = []
    for path in sorted((REPO / "stone_pipeline").rglob("*.py")):
        if "tests" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        if re.search(r"category\s*=\s*'category'", src) or re.search(r"attribute_id\([^)]*\"category\"", src):
            hits.append(str(path.relative_to(REPO)))
    assert hits == []


def test_the_registry_reads_t1_by_handle_never_by_label():
    src = (REPO / "stone_pipeline/config/categories.py").read_text(encoding="utf-8")
    assert not re.search(r"_pcat\(\s*c(\[\"label\"\]|\.label)", src)
    assert not re.search(r"\{a\.value: a\.sourceid", src)
