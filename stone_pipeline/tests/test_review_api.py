"""The config server's review API: the :4200 admin reads the pending queue and writes mint/reject/alias
decisions + attribute ids. Pure dispatch tests (no sockets), mirroring test_config_store.py. The variety
vocabulary is monkeypatched so the alias-validation tests do not depend on the committed backbone."""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, server, varieties
from stone_pipeline.stages import decisions


@pytest.fixture
def seeded_queue():
    """A produce has surfaced one uncertain variety + one new attribute value."""
    decisions.write_confirm_file([
        {"confirm": "", "variant": "Zucchi Blue X", "stone_type": "granite", "color": "blue",
         "nearest_existing": "Azul X", "score": 0.72, "model_prob": 0.61}])
    decisions.write_attributes_to_add([
        {"kind": "finish", "value": "Leathered", "count": 12, "suggested_value": "", "action": "",
         "medusa_id": ""}])


def test_get_pending_variants_and_attributes(seeded_queue):
    code, body = server.dispatch("GET", ["review", "variants"], None)
    assert code == 200
    assert [v["variant"] for v in body["variants"]] == ["Zucchi Blue X"]
    assert body["variants"][0]["nearest_existing"] == "Azul X"

    code, body = server.dispatch("GET", ["review", "attributes"], None)
    assert code == 200 and [a["value"] for a in body["attributes"]] == ["Leathered"]


def test_pending_variant_carries_src_url_to_the_endpoint():
    # Regression (Blokport proof): the review-variant item must emit src_url end-to-end, not just src (the
    # source code). It was dropped because src_url was absent from CONFIRM_COLUMNS -> the payload allowlist.
    decisions.write_confirm_file([
        {"confirm": "", "variant": "Zucchi Blue X", "stone_type": "granite", "src": "zucchi",
         "src_url": "https://inventory.zucchistones.com/product?Zucchi-Blue-X-BD00000001"}])
    code, body = server.dispatch("GET", ["review", "variants"], None)
    assert code == 200
    item = body["variants"][0]
    assert item["src_url"] == "https://inventory.zucchistones.com/product?Zucchi-Blue-X-BD00000001"
    assert item["src"] == "zucchi"   # both present: src is the code, src_url is the product page


def test_put_bad_action_is_400():
    code, body = server.dispatch("PUT", ["review", "variants", "Foo"], {"action": "frobnicate"})
    assert code == 400 and "action" in body["error"]
    assert server.dispatch("PUT", ["review", "variants", "Foo"], "not-a-dict")[0] == 400


def test_put_attribute_id(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "attributes", "Leathered"],
                                 {"kind": "finish", "medusa_id": "pcol_9"})
    assert code == 200 and body["medusa_id"] == "pcol_9"
    assert decisions.load_attribute_ids() == {("finish", "leathered"): ("Leathered", "pcol_9")}
    # missing id is refused
    assert server.dispatch("PUT", ["review", "attributes", "Honed"], {"kind": "finish"})[0] == 400


def test_get_varieties_route(monkeypatch):
    monkeypatch.setattr(varieties, "list_all",
                        lambda q=None, limit=None: [{"name": "Bianco Carrara", "stone_type": "Marble"}])
    code, body = server.dispatch("GET", ["varieties"], None)
    assert code == 200 and body["varieties"][0]["name"] == "Bianco Carrara"


def test_unknown_review_subpath_is_404():
    assert server.dispatch("GET", ["review", "nonsense"], None)[0] == 404


def test_get_adapters_lists_the_registry():
    # ISS-3 dropdown source: the coded adapters available to run. Lets :4200 validate the "adapter" field
    # instead of parsing a 400 error. The real registry is the coded sources (fuleistone added, PR #185).
    code, body = server.dispatch("GET", ["adapters"], None)
    assert code == 200
    assert set(body["adapters"]) == {"marenostone", "polonine", "varsha", "zucchi", "fuleistone"}
    assert server.dispatch("POST", ["adapters"], {})[0] == 405


# -- mint seed colour (seed a colourless new variety with a real colour, not 'Natural') --------

def test_get_colors_returns_the_medusa_vocab():
    code, body = server.dispatch("GET", ["colors"], None)
    assert code == 200
    assert "Beige" in body["colors"] and body["colors"] == sorted(body["colors"])   # canonical, sorted


# -- mint seed type (assign a stone type to a type-less new variety; HELD until it has one) --------

def test_get_types_returns_the_medusa_vocab():
    code, body = server.dispatch("GET", ["types"], None)
    assert code == 200
    assert "Granite" in body["types"] and body["types"] == sorted(body["types"])   # canonical, sorted


# -- mint seed country (Q2: the operator picks the ORIGIN at approval; it overlays origin_map) --------


# --- reject is the one explicit action; everything else is a statement (/review/decide) ------------

def test_put_reject_stores_and_reflects(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], {"action": "reject"})
    assert code == 200 and body["action"] == "reject"
    assert decisions.load_rejected() == {"zucchi blue x"}
    v = server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]
    assert v["current_action"] == "reject"


def test_put_reject_with_percent_encoded_name_keys_on_the_real_name():
    decisions.write_confirm_file([{"confirm": "", "variant": "Alpine Luxe", "stone_type": "", "color": ""}])
    code, body = server.dispatch("PUT", ["review", "variants", "Alpine%20Luxe"], {"action": "reject"})
    assert code == 200 and body["variant"] == "Alpine Luxe"
    assert server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]["current_action"] == "reject"


def test_put_mint_or_alias_bodies_are_gone(seeded_queue):
    for body in ({"action": "mint"}, {"action": "alias", "alias_of": "X"}, {"action": "mint", "color": "beige"}):
        code, out = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], body)
        assert code == 400 and "decide" in out["error"]
    assert decisions.load_confirm_decisions() == {}


def test_origins_routes_are_gone():
    assert server.dispatch("GET", ["review", "origins"], None)[0] == 404
    assert server.dispatch("PUT", ["review", "origins", "a|b|c"], {"country_iso": "IR"})[0] == 404


def test_decide_colour_must_be_a_real_attribute(monkeypatch):
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: False)
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": "Zucchi Blue X", "name": "Zucchi Blue X",
                                  "type": "Granite", "color": "definitely-not-a-colour"})
    assert code == 400 and "colour" in body["error"]
    assert decisions.load_variety_seed_colors() == {}
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": "Zucchi Blue X", "name": "Zucchi Blue X",
                                  "type": "Granite", "color": "beige", "origin": "brazil"})
    assert code == 200 and body["color"] == "Beige" and body["origin"] == "BR"
    assert decisions.load_variety_seed_colors() == {"zucchi blue x": "Beige"}
    assert decisions_store.variety_seed_countries() == {"zucchi blue x": "BR"}
