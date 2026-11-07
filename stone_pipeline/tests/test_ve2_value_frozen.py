"""VE2 PR4: restricted attribute values on scraped products (design section 3 + the 11-06 ruling).

validate's `value_frozen` rule HOLDS a scraped product whose resolved colour/finish/quality/type is archived
or internal, unless Medusa already imported THIS product (acked) carrying it. Restricted values come from
attributes.csv through medusa_files.read_attributes (restricted_values), the single authority: there is no
ledger column. Pack defaults must be assignable or the produce fails loud. No learned alias is saved onto an
archived Key. Recognition keeps the full vocabulary (archived values stay matchable).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.existing import acked_products
from stone_pipeline.reference.loaders import restricted_values
from stone_pipeline.reference.loaders.common import norm
from stone_pipeline.stages import validate


# --- restricted_values: the single authority (the files) --------------------

def test_restricted_values_reads_archived_and_internal(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid,status,internal\n"
                 "color,Beige,c1,active,false\n"
                 "finish,Polished,f1,archived,false\n"
                 "color,SecretBlue,c2,active,true\n", encoding="utf-8")
    assert restricted_values(p) == {"finish": {norm("Polished")}, "color": {norm("SecretBlue")}}


def test_restricted_values_empty_without_status_columns(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid\ncolor,Beige,c1\n", encoding="utf-8")
    assert restricted_values(p) == {}   # expand phase: nothing is restricted until the columns exist


def test_restricted_values_absent_file(tmp_path):
    assert restricted_values(tmp_path / "none.csv") == {}


# --- acked_products: Medusa-held values exempt an existing product -----------

def _ledger_product(lg, sku, *, color=None, finish=None, quality=None, type_=None, category=None, medusa_id=None):
    now = now_iso()
    lg.upsert("product", {"sku": sku, "source": sku.split("-")[0].lower(), "surrogate_key": sku.split("-")[-1],
                          "color": color, "finish": finish, "quality": quality, "type": type_,
                          "category": category, "medusa_id": medusa_id, "state": "synced",
                          "created_at": now, "updated_at": now}, pk=("sku",))


def test_acked_products_only_includes_medusa_held(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _ledger_product(lg, "ZUCCHI-1", color="Beige", finish="Polished", medusa_id="M1")
        _ledger_product(lg, "ZUCCHI-2", color="Grey", medusa_id=None)   # not acked: never exempt
        out = acked_products(lg)
    assert out == {"ZUCCHI-1": {("color", norm("Beige")), ("finish", norm("Polished"))}}


def test_acked_products_includes_category(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _ledger_product(lg, "ZUCCHI-1", category="Tiles", medusa_id="M1")
        out = acked_products(lg)
    assert out == {"ZUCCHI-1": {("category", norm("Tiles"))}}


# --- validate value_frozen ---------------------------------------------------

def _row(color="Beige"):
    return CanonicalRow(src_site="zucchi", surrogate_key="1", color_name=color, color_id="c1")


def test_value_frozen_holds_a_scraped_product_on_a_restricted_value():
    row = _row("Beige")
    validate.validate_row(row, frozen={"color": {norm("Beige")}}, acked={}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}
    assert row.color_name == "Beige"   # HELD, never re-resolved to a nearby active value


def test_value_frozen_exempts_an_existing_acked_product():
    row = _row("Beige")
    validate.validate_row(row, frozen={"color": {norm("Beige")}},
                          acked={"ZUCCHI-1": {("color", norm("Beige"))}}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_value_frozen_holds_when_there_is_no_ledger():
    # no ledger (local / CI): acked is empty, so a restricted value is held even for a product that might
    # already exist in Medusa -- the conservative, correct default.
    row = _row("Beige")
    validate.validate_row(row, frozen={"color": {norm("Beige")}}, acked={}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_value_frozen_ignores_an_active_value():
    row = _row("Beige")
    validate.validate_row(row, frozen={"color": {norm("Polished")}}, acked={}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_value_frozen_dormant_without_a_frozen_set():
    # the expand phase: no restricted values, so the rule never fires and the ledger is never consulted
    row = _row("Beige")
    validate.validate_row(row, frozen={}, acked={}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_value_frozen_needs_source_code_when_something_is_frozen():
    # a missing source_code must fail loud, never silently exempt no one / hold all
    with pytest.raises(ValueError):
        validate.validate_row(_row("Beige"), frozen={"color": {norm("Beige")}}, acked={}, source_code=None)


def test_value_frozen_exemption_is_value_specific():
    # acked with Beige, but the scrape now resolves to a DIFFERENT restricted colour: held (not exempt)
    row = _row("Crimson")
    validate.validate_row(row, frozen={"color": {norm("Crimson")}},
                          acked={"ZUCCHI-1": {("color", norm("Beige"))}}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_value_frozen_holds_a_new_product_in_an_archived_category():
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tiles")
    validate.validate_row(row, frozen={"category": {norm("Tiles")}}, acked={},
                          category_labels={"pcat_tiles": "Tiles"}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_value_frozen_exempts_an_existing_product_in_an_archived_category():
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tiles")
    validate.validate_row(row, frozen={"category": {norm("Tiles")}},
                          acked={"ZUCCHI-1": {("category", norm("Tiles"))}},
                          category_labels={"pcat_tiles": "Tiles"}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_a_row_on_a_pcat_absent_from_attributes_is_not_held_or_crashed():
    # a category pcat with no attributes.csv row can never be restricted (restricted_values comes only from
    # those rows), so it returns None. One such row (an env-override pcat) must NOT raise or hold while another
    # value is restricted -- one dead row never crashes the source's produce.
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="env_override_pcat",
                       color_name="Grey")
    validate.validate_row(row, frozen={"color": {norm("Pink")}}, acked={},
                          category_labels={}, source_code="zucchi")   # must not raise
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_category_labels_come_from_attributes_not_the_registry(monkeypatch):
    # finding 1: the label map is sourced from attributes.csv (the same table the row's pcat comes from), so
    # an env override on the Category REGISTRY pcat can never make the category silently skip the check.
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.reference.medusa_files import read_attributes
    if not SETTINGS.paths.attributes_csv.exists():
        pytest.skip("no attributes.csv")
    cats = {a.sourceid: a.value for a in read_attributes(SETTINGS.paths.attributes_csv, require_status=False)
            if a.category == "category" and a.sourceid}
    if not cats:
        pytest.skip("no category rows in attributes.csv")
    monkeypatch.setattr("stone_pipeline.reference.loaders.restricted_values",
                        lambda *a, **k: {"color": {norm("Beige")}})
    monkeypatch.setenv("BLOKPORT_CAT_TILES_PCAT", "an-override-that-differs-from-attributes")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(SETTINGS.paths.workspace_root / "nonexistent.ledger"))
    _frozen, _acked, category_labels = validate._frozen_and_acked("zucchi")
    assert category_labels == cats   # straight from attributes.csv, unaffected by the registry override


def test_frozen_and_acked_with_no_ledger_holds_everything(tmp_path, monkeypatch):
    monkeypatch.setattr("stone_pipeline.reference.loaders.restricted_values",
                        lambda *a, **k: {"color": {norm("Beige")}})
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "nonexistent.ledger"))
    frozen, acked, _cats = validate._frozen_and_acked("zucchi")
    assert frozen and acked == {}   # no ledger -> nobody exempt -> every restricted value held


def test_frozen_and_acked_reads_a_real_ledger(tmp_path, monkeypatch):
    from stone_pipeline.ledger import writethrough
    lpath = tmp_path / "dev.ledger"
    with Ledger.open(lpath, env=writethrough.ENV_NAME,
                     backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        _ledger_product(lg, "ZUCCHI-1", color="Beige", medusa_id="M1")
    monkeypatch.setattr("stone_pipeline.reference.loaders.restricted_values",
                        lambda *a, **k: {"color": {norm("Beige")}})
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(lpath))
    _frozen, acked, _cats = validate._frozen_and_acked("zucchi")
    assert acked.get("ZUCCHI-1") == {("color", norm("Beige"))}


def test_frozen_and_acked_raises_on_a_corrupt_ledger(tmp_path, monkeypatch):
    # with something restricted, _frozen_and_acked opens the ledger; a corrupt one must fail loud, not be
    # swallowed into an empty exemption (which would hold every acked product too).
    monkeypatch.setattr("stone_pipeline.reference.loaders.restricted_values",
                        lambda *a, **k: {"color": {norm("Beige")}})
    bad = tmp_path / "dev.ledger"
    bad.write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(bad))
    with pytest.raises(sqlite3.DatabaseError):
        validate._frozen_and_acked("zucchi")


@pytest.mark.parametrize("surrogate, expected", [
    ("A1", "ZUCCHI-A1"),
    (None, "ZUCCHI-NONE"),      # no coercion: str(None).upper(); re-adding `or ""` would give "ZUCCHI-"
    ("a1", "ZUCCHI-A1"),        # upper-cased
    (" a1 ", "ZUCCHI- A1 "),    # not stripped
])
def test_emit_ledger_sku_equals_the_emitted_variant_sku(surrogate, expected):
    # 5a: one SKU rule, so the ledger key (populate via emit._sku) and the emitted Variant Sku (sku_for) are
    # identical for the same row AND equal the exact expected value -- re-adding any coercion fails this.
    from stone_pipeline.stages import emit
    from stone_pipeline.stages.product_state import sku_for
    cfg = SimpleNamespace(source_code="zucchi")
    row = CanonicalRow(src_site="zucchi", surrogate_key=surrogate)
    assert emit._sku(row, cfg) == sku_for(row, cfg) == expected


def test_a_restricted_palette_colour_does_not_fail_load(monkeypatch):
    # the classify palette is PR5's concern (a restricted colour is dropped from a mint), NOT a pack default,
    # so an archived palette colour must not abort every load_all.
    from stone_pipeline.config.domain import active_pack
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.reference import loaders
    from stone_pipeline.stages.variety_color import CLASSIFIABLE_COLORS

    pack = active_pack()
    if not pack.classify_texture_color or not SETTINGS.paths.attributes_csv.exists():
        pytest.skip("pack does not classify texture colour, or no attributes.csv")
    palette_only = next((c for c in CLASSIFIABLE_COLORS if norm(c) != norm(pack.fallback_color)), None)
    if palette_only is None:
        pytest.skip("no palette colour distinct from the fallback colour")
    ref = SimpleNamespace(attributes=loaders.load_attributes())
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.restricted_values",
                        lambda *a, **k: {"color": {norm(palette_only)}})
    loaders._assert_pack_defaults_resolve(ref)   # must NOT raise: a palette colour is not a pack default


# --- pack defaults must be assignable (fail loud) ----------------------------

def test_a_restricted_pack_default_fails_loud(monkeypatch):
    from stone_pipeline.config.domain import active_pack
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.reference import loaders
    if not SETTINGS.paths.attributes_csv.exists():
        pytest.skip("no attributes.csv to resolve the pack defaults against")
    ref = SimpleNamespace(attributes=loaders.load_attributes())
    loaders._assert_pack_defaults_resolve(ref)   # today: every default resolves AND is assignable
    # now pretend the mint fallback colour is archived/internal: a new product falling back to it would be
    # held forever, so load must fail loud, naming it.
    fallback = active_pack().fallback_color
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.restricted_values",
                        lambda *a, **k: {"color": {norm(fallback)}})
    with pytest.raises(ValueError, match=fallback):
        loaders._assert_pack_defaults_resolve(ref)


# --- no learned alias onto an archived Key (recognition unchanged) -----------

def test_a_fuzzy_bind_onto_an_archived_variety_is_recognized_but_not_learned():
    from stone_pipeline.matching.engine import VariationEngine
    from stone_pipeline.matching.index import CandidateIndex
    from stone_pipeline.stages import match_variation
    from stone_pipeline.state import writeback as wb
    from stone_pipeline.tests.test_origin_aware_matching import _stage

    idx = CandidateIndex()
    idx.add("a", "Kashmir Golda", surfaces=[], block_type="granite", origins={"BR"})
    stage = match_variation.VariationStage(
        ref=SimpleNamespace(**{**vars(_stage().ref), "variants": {"slab": SimpleNamespace(
            by_id={"a": SimpleNamespace(key="slab_granite_kashmir_golda_1", archived=True)})}}),
        engines={"slab": VariationEngine(idx, auto_accept=90, review_floor=80)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Goldx",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id == "a" and row.variation_method == "fuzzy"   # still RECOGNIZED
    assert stage.writeback.aliases == []                                 # but the guess is NOT learned onto it
