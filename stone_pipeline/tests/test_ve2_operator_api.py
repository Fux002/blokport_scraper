"""VE2 operator gate (design section 7): the review API never OFFERS or ASSIGNS a restricted (archived or
internal) value, and never binds a listing onto an archived variety -- but a statement that only RE-CONFIRMS
what a record already carries is a no-op and is allowed. Everything here is dormant until Blokport publishes
the status columns: with nothing restricted and no variety archived, every response is byte-identical to
before. Self-contained: stubbed variety lookups, a tmp config.db, patched vocab + restricted sets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stone_pipeline.config import decisions_store, store, varieties
from stone_pipeline.config.control import resolved, server
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import _norm
from stone_pipeline.reference.loaders.backbone import Backbone, BackboneVariety

EXISTING = {("golden lightning", "granite"), ("azul white", "onyx"), ("brown granite", "granite")}

TYPES = {"granite": "Granite", "onyx": "Onyx", "quartzite": "Quartzite", "marble": "Marble"}
COLORS = {"white": "White", "green": "Green", "smoke": "Smoke", "blue": "Blue"}


def _exists(n, t):
    return (_norm(n), _norm(t)) in EXISTING


def _backbone(*varieties_):
    bb = Backbone()
    for v in varieties_:
        bb.by_norm_name.setdefault(_norm(v.variant), []).append(v)
    return bb


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "config_db_path", lambda: tmp_path / "config.db")
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: _exists(n, t))
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: False)     # a test opts a variety into archived
    monkeypatch.setattr(server, "_type_vocab", lambda: dict(TYPES))
    monkeypatch.setattr(server, "_color_vocab", lambda: dict(COLORS))
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {})  # dormant; a test opts a value in
    monkeypatch.setattr(loaders, "restricted_ids", lambda *a, **k: set())
    monkeypatch.setattr(loaders, "load_backbone", lambda *a, **k: Backbone())  # the real seed never leaks in
    monkeypatch.setattr(resolved, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(outputs_dir=tmp_path / "no-outputs")))
    yield


def _decide(**over):
    body = {"source": "marenostone", "scraped": "New Spelling", "name": "Brand New", "type": "Onyx"}
    body.update(over)
    return server.dispatch("PUT", ["review", "decide"], body)


# -- pickers: assignable only --------------------------------------------------------------------------------

def test_color_picker_drops_restricted_values(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"color": {"smoke"}})
    code, body = server.dispatch("GET", ["colors"], None)
    assert code == 200 and body["colors"] == ["Blue", "Green", "White"]   # Smoke withheld, still sorted


def test_type_picker_drops_restricted_values(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"type": {"marble"}})
    code, body = server.dispatch("GET", ["types"], None)
    assert code == 200 and body["types"] == ["Granite", "Onyx", "Quartzite"]


def test_pickers_are_dormant_without_a_status_column():
    assert server.dispatch("GET", ["colors"], None)[1]["colors"] == ["Blue", "Green", "Smoke", "White"]
    assert server.dispatch("GET", ["types"], None)[1]["types"] == ["Granite", "Marble", "Onyx", "Quartzite"]


# -- attribute paste: a restricted id is refused -------------------------------------------------------------

def test_attribute_paste_of_a_restricted_id_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(decisions_store, "set_attribute_id", lambda k, v, m: calls.append((k, v, m)))
    monkeypatch.setattr(loaders, "restricted_ids", lambda *a, **k: {"pcol_dead"})
    code, body = server.dispatch("PUT", ["review", "attributes", "Smoke"], {"kind": "color", "medusa_id": "pcol_dead"})
    assert code == 400 and body["restricted"] and calls == []              # never reached the store


def test_attribute_paste_is_dormant_for_an_assignable_id(monkeypatch):
    calls = []
    monkeypatch.setattr(decisions_store, "set_attribute_id", lambda k, v, m: calls.append((k, v, m)))
    code, body = server.dispatch("PUT", ["review", "attributes", "Smoke"], {"kind": "color", "medusa_id": "pcol_live"})
    assert code == 200 and calls == [("color", "Smoke", "pcol_live")]


# -- decide, MINT: the new variety's type and colour must be assignable --------------------------------------

def test_mint_with_a_restricted_type_is_refused(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"type": {"onyx"}})
    code, body = _decide(type="Onyx")
    assert code == 400 and body["restricted"] and "type" in body["error"]


def test_mint_with_a_restricted_colour_is_refused(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"color": {"smoke"}})
    code, body = _decide(type="Onyx", color="Smoke")
    assert code == 400 and body["restricted"] and "colour" in body["error"]


def test_mint_with_an_assignable_type_and_colour_is_allowed(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"type": {"marble"}, "color": {"smoke"}})
    code, body = _decide(type="Onyx", color="White")
    assert code == 200 and body["result"] == "minted"


# -- decide, BIND onto an archived variety -------------------------------------------------------------------

def test_bind_onto_an_archived_variety_is_refused(monkeypatch):
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: (_norm(n), _norm(t)) == ("golden lightning", "granite"))
    code, body = _decide(scraped="Amazon Green Granite", name="Golden Lightning", type="Granite")
    assert code == 400 and body["archived"] and "Golden Lightning" in body["error"]


def test_an_identical_reconfirm_of_a_stored_vendor_bind_is_allowed(monkeypatch):
    # the bind was stored while the variety was still active; archiving it must not break re-confirming it
    decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite", exists_as=_exists)
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: (_norm(n), _norm(t)) == ("golden lightning", "granite"))
    code, body = _decide(scraped="Amazon Green Granite", name="Golden Lightning", type="Granite")
    assert code == 200 and body["result"] == "bound"
    # a statement that is NOT identical (a different origin) is still refused
    code, body = _decide(scraped="Amazon Green Granite", name="Golden Lightning", type="Granite", origin="Iran")
    assert code == 400 and body["archived"]


def test_reconfirm_of_a_global_mint_is_allowed(monkeypatch):
    # the first mint on a spelling is stored GLOBALLY at ('', spelling); after the variety is archived,
    # re-saving the same statement must re-confirm off that global row, not false-400 for want of a vendor row
    decisions_store.decide("marenostone", "New Quartz", "New Quartz", "Quartzite", exists_as=_exists)
    monkeypatch.setattr(varieties, "exists_as",
                        lambda n, t: _exists(n, t) or (_norm(n), _norm(t)) == ("new quartz", "quartzite"))
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: (_norm(n), _norm(t)) == ("new quartz", "quartzite"))
    code, _ = _decide(source="marenostone", scraped="New Quartz", name="New Quartz", type="Quartzite")
    assert code == 200


def test_reconfirm_with_a_changed_colour_is_refused(monkeypatch):
    # a stored bind carries White; re-saving it with a different colour onto the now-archived variety is NOT a
    # re-confirm (the old bug accepted it and rewrote the variety's colour) -> refused
    decisions_store.decide("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite",
                           color="White", exists_as=_exists)
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: (_norm(n), _norm(t)) == ("golden lightning", "granite"))
    code, body = _decide(scraped="Amazon Green Granite", name="Golden Lightning", type="Granite", color="Green")
    assert code == 400 and body["archived"]


def test_a_bind_onto_an_archived_alias_target_is_refused(monkeypatch):
    # the operator states an ALIAS; it resolves to the archived canonical variety -> refused by the canonical
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: _exists(n, t) or (_norm(n), _norm(t)) == ("andes", "quartzite"))
    monkeypatch.setattr(varieties, "alias_target",
                        lambda n, t: "Andes" if (_norm(n), _norm(t)) == ("artemis", "quartzite") else None)
    monkeypatch.setattr(varieties, "is_archived", lambda n, t: (_norm(n), _norm(t)) == ("andes", "quartzite"))
    code, body = _decide(scraped="ARTEMIS", name="Artemis", type="Quartzite")
    assert code == 400 and body["archived"] and "Andes" in body["error"]


# -- decide, COLOUR AMEND on an existing variety -------------------------------------------------------------

def test_amending_an_existing_variety_to_a_restricted_colour_is_refused(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"color": {"smoke"}})
    # Golden Lightning documents White; Smoke is a NEW, restricted colour -> an assignment -> refused
    decisions_store.decide("marenostone", "Golden Lightning", "Golden Lightning", "Granite", color="White", exists_as=_exists)
    code, body = _decide(source="zucchi", scraped="GL", name="Golden Lightning", type="Granite", color="Smoke")
    assert code == 400 and body["restricted"] and "colour" in body["error"]


def test_restating_a_varietys_own_restricted_stated_colour_is_allowed(monkeypatch):
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"color": {"smoke"}})
    # Golden Lightning already documents Smoke via an operator statement (now restricted); carrying it is no amend
    decisions_store.decide("marenostone", "Golden Lightning", "Golden Lightning", "Granite", color="Smoke", exists_as=_exists)
    code, body = _decide(source="zucchi", scraped="GL", name="Golden Lightning", type="Granite", color="Smoke")
    assert code == 200 and body["result"] == "bound"


def test_restating_a_varietys_backbone_colour_is_allowed(monkeypatch):
    # the variety's current colour lives in the BACKBONE seed (+ leaf overlay), not an operator statement;
    # restating that colour, even once it is restricted, is not an assignment
    monkeypatch.setattr(loaders, "restricted_values", lambda *a, **k: {"color": {"smoke"}})
    seed = BackboneVariety(variant="Golden Lightning", category="slab", stone_type="Granite",
                           colors=["Smoke"], finishes=[], qualities=[], aliases=[])
    monkeypatch.setattr(loaders, "load_backbone", lambda *a, **k: _backbone(seed))
    code, body = _decide(source="zucchi", scraped="GL", name="Golden Lightning", type="Granite", color="Smoke")
    assert code == 200 and body["result"] == "bound"


# -- dormant: with no status column the gate changes nothing -------------------------------------------------

def test_decide_is_dormant_without_a_status_column():
    # a mint and a bind both succeed; nothing is restricted and no variety is archived
    assert _decide(type="Onyx")[1]["result"] == "minted"
    code, body = _decide(scraped="Amazon Green Granite", name="Golden Lightning", type="Granite")
    assert code == 200 and body["result"] == "bound"
