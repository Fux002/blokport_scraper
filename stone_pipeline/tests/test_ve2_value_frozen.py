"""Restricted attribute values on scraped products.

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
from stone_pipeline.tests._category_registry import set_category_pcats


# --- restricted_values: the single authority (the files) --------------------

def test_restricted_values_reads_archived_and_internal(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid,status,internal,handle\n"
                 "color,Beige,c1,active,false\n"
                 "finish,Polished,f1,archived,false\n"
                 "color,SecretBlue,c2,active,true\n", encoding="utf-8")
    assert restricted_values(p) == {"finish": {norm("Polished")}, "color": {norm("SecretBlue")}}


def test_restricted_values_empty_without_status_columns(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid,handle\ncolor,Beige,c1\n", encoding="utf-8")
    assert restricted_values(p) == {}   # expand phase: nothing is restricted until the columns exist


def test_restricted_values_absent_file(tmp_path):
    assert restricted_values(tmp_path / "none.csv") == {}


# --- acked_products: Medusa-held values exempt an existing product -----------

def _ledger_product(lg, sku, *, color=None, finish=None, quality=None, type_=None, category=None,
                    variation_key=None, medusa_id=None):
    now = now_iso()
    lg.upsert("product", {"sku": sku, "source": sku.split("-")[0].lower(), "surrogate_key": sku.split("-")[-1],
                          "color": color, "finish": finish, "quality": quality, "type": type_,
                          "category": category, "variation_key": variation_key, "medusa_id": medusa_id,
                          "state": "synced", "created_at": now, "updated_at": now}, pk=("sku",))


def test_acked_products_only_includes_medusa_held(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _ledger_product(lg, "ZUCCHI-1", color="Beige", finish="Polished", medusa_id="M1")
        _ledger_product(lg, "ZUCCHI-2", color="Grey", medusa_id=None)   # not acked: never exempt
        out = acked_products(lg)
    assert out == {"ZUCCHI-1": {("color", norm("Beige")), ("finish", norm("Polished"))}}


def test_acked_products_carry_the_stored_category_handle(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _ledger_product(lg, "ZUCCHI-1", category="tiles", medusa_id="M1")   # stored by handle (D11)
        out = acked_products(lg)
    assert out == {"ZUCCHI-1": {("category", "tiles")}}                     # the handle itself


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


def test_value_frozen_holds_a_new_product_in_an_archived_category(monkeypatch):
    set_category_pcats(monkeypatch, {"tile": "pcat_tiles"})       # the row's pcat is the registry's (577)
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tiles")
    validate.validate_row(row, frozen={"category": {"tiles"}}, acked={}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_value_frozen_exempts_an_existing_product_in_an_archived_category(monkeypatch):
    set_category_pcats(monkeypatch, {"tile": "pcat_tiles"})
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_tiles")
    validate.validate_row(row, frozen={"category": {"tiles"}},
                          acked={"ZUCCHI-1": {("category", "tiles")}}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_a_row_on_a_pcat_no_category_has_is_not_held_or_crashed():
    # a pcat no active category has (category_invalid refuses that row on its own) carries no category to
    # freeze, so it returns None: it must NOT raise or hold while another value is restricted -- one dead row
    # never crashes the source's produce.
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", category_pcat_id="pcat_no_category",
                       color_name="Grey")
    validate.validate_row(row, frozen={"color": {norm("Pink")}}, acked={}, source_code="zucchi")   # must not raise
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_frozen_and_acked_with_no_ledger_holds_everything(tmp_path, monkeypatch):
    monkeypatch.setattr("stone_pipeline.reference.loaders.restricted_values",
                        lambda *a, **k: {"color": {norm("Beige")}})
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "nonexistent.ledger"))
    frozen, acked = validate._frozen_and_acked("zucchi")
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
    _frozen, acked = validate._frozen_and_acked("zucchi")
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
    # the classify palette is the mint's concern (a restricted colour is dropped from a mint), NOT a pack default,
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
            by_id={"a": SimpleNamespace(key="granite__kashmir_golda_1", archived=True)})}}),
        engines={"slab": VariationEngine(idx, auto_accept=90, review_floor=80)}, writeback=wb.WriteBack(),
        source_cfg=SimpleNamespace(primary_origin=""))
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", variety_match_key="Kashmir Goldx",
                       raw_type="Granite", raw_format="Slab")
    stage.resolve_row(row)
    assert row.variation_id == "a" and row.variation_method == "fuzzy"   # still RECOGNIZED
    assert stage.writeback.aliases == []                                 # but the guess is NOT learned onto it


# --- the variety Key as a frozen dimension ------------------------------------

_ARCH_KEY = "marble__carrara_1"


def _vrow():
    return CanonicalRow(src_site="zucchi", surrogate_key="1", variation_key=_ARCH_KEY)


def _variation_row(lg, key):
    now = now_iso()
    lg.upsert("variation", {"key": key, "type": "Marble", "name": "x",
                            "aliases": "[]",
                            "medusa_id": "V", "in_full": 1, "payload_hash": "", "state": "synced",
                            "archived": 0, "first_seen": now, "last_synced": now, "created_at": now,
                            "updated_at": now}, pk=("key",))


def test_acked_products_includes_the_variation_key(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation_row(lg, _ARCH_KEY)   # product.variation_key is a FK to variation.key
        _ledger_product(lg, "ZUCCHI-1", variation_key=_ARCH_KEY, medusa_id="M1")
        out = acked_products(lg)
    assert ("variation_key", norm(_ARCH_KEY)) in out["ZUCCHI-1"]


def test_a_new_product_on_an_archived_variety_is_held():
    row = _vrow()
    validate.validate_row(row, frozen={"variation_key": {norm(_ARCH_KEY)}}, acked={}, source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_an_existing_product_staying_on_its_archived_variety_is_exempt():
    row = _vrow()
    validate.validate_row(row, frozen={"variation_key": {norm(_ARCH_KEY)}},
                          acked={"ZUCCHI-1": {("variation_key", norm(_ARCH_KEY))}}, source_code="zucchi")
    assert "value_frozen" not in {r.rule for r in row.reject_reasons}


def test_an_existing_product_moving_onto_an_archived_variety_is_held():
    row = _vrow()   # resolved to the archived Key, but acked on a DIFFERENT key -> a move -> held
    validate.validate_row(row, frozen={"variation_key": {norm(_ARCH_KEY)}},
                          acked={"ZUCCHI-1": {("variation_key", norm("marble__other_2"))}},
                          source_code="zucchi")
    assert "value_frozen" in {r.rule for r in row.reject_reasons}


def test_a_variation_key_hold_is_visible_in_held_breakdown():
    # fix 4: a scrape bound to an archived variety (its alias is emitted as normal, no dead-end gap) ends HELD
    # by the value_frozen variation_key freeze, and that hold is visible in held_breakdown for the operator.
    row = _vrow()
    validate.validate_row(row, frozen={"variation_key": {norm(_ARCH_KEY)}}, acked={}, source_code="zucchi")
    assert any(e["rule"] == "value_frozen" for e in validate.held_breakdown([row]))
