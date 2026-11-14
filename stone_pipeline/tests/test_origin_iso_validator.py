"""Every country code the scraper accepts is a real ISO-3166 alpha-2 code: a bad one is refused loudly where it is
loaded or written, naming the value, never carried on to be dropped or emitted later."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from stone_pipeline.config import decisions_store, store
from stone_pipeline.config.control import server
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import reference_data, resolve_iso
from stone_pipeline.reference.loaders.origins import OriginMap, OriginOverrides, OriginRule
from stone_pipeline.stages import derive

VALID = frozenset({"BR", "IT", "IR", "TR"})


def _invalid_country():
    from stone_pipeline.reference.loaders.origins import InvalidCountry
    return InvalidCountry


def _map_with(code):
    return lambda *a, **k: OriginMap(rules=[OriginRule(variety="Foo", country_iso=code, city="", county="",
                                                       stone_type="Granite")])


def _overrides_with(code):
    return lambda *a, **k: OriginOverrides(rules={("src", "foo", "granite"): code})


def _decisions(**legacy):
    return lambda *a, **k: Decisions.from_legacy(**legacy)


_MINT = {("src", "foo"): {"action": "mint", "seed_name": "Foo", "seed_type": "Granite", "seed_country": "XX"}}


@pytest.mark.parametrize("label, patch", [
    ("origin_map.csv", lambda mp, tmp: mp.setattr(reference_data, "load_origin_map", _map_with("BR,XX"))),
    ("origin_supplier_overrides.csv",
     lambda mp, tmp: mp.setattr(reference_data, "load_origin_overrides", _overrides_with("XX"))),
    ("mint origins", lambda mp, tmp: mp.setattr(decisions_store, "load_decisions", _decisions(actions=_MINT))),
    ("widened origins", lambda mp, tmp: mp.setattr(
        decisions_store, "load_decisions", _decisions(actions={}, widen={("src", "foo", "granite"): "XX"}))),
    ("vendor origin confirmations", lambda mp, tmp: mp.setattr(
        decisions_store, "load_decisions", _decisions(actions={}, origins={("src", "foo", "granite"): "XX"}))),
])
def test_a_non_iso_origin_refuses_the_reference_load_naming_its_source_and_value(monkeypatch, tmp_path, label, patch):
    patch(monkeypatch, tmp_path)
    with pytest.raises(_invalid_country(), match=f"{label}.*'XX'"):
        loaders.load_all()


def test_a_non_iso_origin_edit_refuses_the_reference_load(monkeypatch, tmp_path):
    db = tmp_path / "config.db"
    db.touch()
    monkeypatch.setattr(store, "config_db_path", lambda: db)
    monkeypatch.setattr(decisions_store, "backbone_leaf_overlay", lambda: {})
    monkeypatch.setattr(decisions_store, "load_decisions", _decisions(actions={}))
    monkeypatch.setattr(decisions_store, "variety_origins", lambda: {("foo", "granite"): "BR,XX"})
    with pytest.raises(_invalid_country(), match="origin edits.*'XX'"):
        loaders.load_all()


# -- source config: origin_default, primary_origin, collection_country --------------------------------------

def _cfg(**over):
    base = SimpleNamespace(origin_default="IT", primary_origin="", collection_country_default="",
                           collection_city_default="")
    return SimpleNamespace(**{**vars(base), **over})


_REF = SimpleNamespace(valid_iso_codes=VALID,
                       to_iso=lambda v: resolve_iso(v, {"italy": "IT"}, VALID))


@pytest.mark.parametrize("field", ["origin_default", "primary_origin"])
@pytest.mark.parametrize("value", ["XX", "Italy", "it", " IT"])
def test_a_source_origin_field_must_be_a_canonical_iso_code(field, value):
    with pytest.raises(_invalid_country(), match=f"{field}.*{value!r}"):
        derive.require_source_countries(_REF, _cfg(**{field: value}))


def test_blank_and_canonical_source_countries_pass():
    derive.require_source_countries(_REF, _cfg(origin_default="", primary_origin=""))
    derive.require_source_countries(_REF, _cfg(origin_default="IT", primary_origin="IR"))


@pytest.mark.parametrize("value", ["XX", "Italy", "it"])
def test_a_collection_country_must_be_a_canonical_iso_code(value):
    with pytest.raises(ValueError, match=f"collection_country_default {value!r}"):
        derive.collection_location(_REF, _cfg(collection_country_default=value, collection_city_default="Verona"))


@pytest.mark.parametrize("field", ["origin_default", "primary_origin"])
def test_a_mistyped_source_origin_holds_the_source_and_keeps_the_last_good_run(
        field, tmp_path, scrape_data_dir, monkeypatch, caplog):
    from stone_pipeline import run as run_module
    from stone_pipeline.config.sources import load_source
    from stone_pipeline.run import run_source
    out = tmp_path / "outputs"
    good = load_source("marenostone")
    monkeypatch.setattr(run_module, "load_source", lambda name: good)
    run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    before = {p: p.read_bytes() for p in out.rglob("canonical.parquet")}
    assert before
    typo = dataclasses.replace(good, **{field: "Itly"})
    monkeypatch.setattr(run_module, "load_source", lambda name: typo)
    with pytest.raises(SystemExit) as stopped, caplog.at_level("ERROR"):
        run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    assert stopped.value.code == 2
    assert "Itly" in caplog.text
    assert {p: p.read_bytes() for p in out.rglob("canonical.parquet")} == before


@pytest.mark.parametrize("field", ["origin_default", "primary_origin", "collection_country"])
def test_the_source_put_refuses_a_non_iso_country_naming_the_field(field):
    body = {"source_code": "POL", "ports": ["port_1"], field: "XX"}
    status, err = server._validate_source_put("polonine", body, None)
    assert status == 400 and field in err["error"] and "'XX'" in err["error"]


@pytest.mark.parametrize("field", ["origin_default", "primary_origin", "collection_country"])
def test_the_source_put_stores_a_country_as_its_iso_code(field):
    body = {"source_code": "POL", "ports": ["port_1"], field: "Italy"}
    assert server._validate_source_put("polonine", body, None) is None
    assert body[field] == "IT"


# -- review API: the existing refusals, pinned ----------------------------------------------------------------

def test_decide_refuses_a_non_iso_origin(monkeypatch):
    monkeypatch.setattr(server, "_type_vocab", lambda: {"granite": "Granite"})
    body = {"source": "marenostone", "scraped": "Foo", "name": "Foo", "type": "Granite", "origin": "XX"}
    status, err = server._put_review_decide({}, body, {})
    assert status == 400 and err["error"] == "origin 'XX' is not a real ISO-3166 country"


def test_edit_origins_refuses_a_non_iso_country(monkeypatch):
    stored = []
    monkeypatch.setattr(decisions_store, "set_variety_origin", lambda *a: stored.append(a))
    body = {"variety": "Foo", "stone_type": "Granite", "countries": ["BR", "XX"]}
    status, err = server._put_origin({}, body, {})
    assert status == 400 and err["error"] == "country 'XX' is not a real ISO-3166 country"
    assert stored == []
