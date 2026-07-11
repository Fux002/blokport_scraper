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


def test_put_mint_then_reject_decision(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], {"action": "mint"})
    assert code == 200 and body["action"] == "mint"
    assert decisions.load_confirm_decisions() == {"zucchi blue x": "yes"}

    code, _ = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], {"action": "reject"})
    assert code == 200
    assert decisions.load_rejected() == {"zucchi blue x"}
    # the pending row now reports the decision (applies on the next produce)
    v = server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]
    assert v["current_action"] == "reject"


def test_put_bad_action_is_400():
    code, body = server.dispatch("PUT", ["review", "variants", "Foo"], {"action": "frobnicate"})
    assert code == 400 and "action" in body["error"]
    assert server.dispatch("PUT", ["review", "variants", "Foo"], "not-a-dict")[0] == 400


def test_put_alias_validates_the_target(monkeypatch):
    # alias onto a NON-existent variety is refused loudly (would be a silent no-op at produce time)
    monkeypatch.setattr(varieties, "exists", lambda name: name == "Bianco Carrara")
    code, body = server.dispatch("PUT", ["review", "variants", "Blue Carara"],
                                 {"action": "alias", "alias_of": "Nope Not Real"})
    assert code == 400 and "not an existing variety" in body["error"]

    code, _ = server.dispatch("PUT", ["review", "variants", "Blue Carara"],
                              {"action": "alias", "alias_of": "Bianco Carrara"})
    assert code == 200
    assert decisions.load_alias_decisions() == {"blue carara": "Bianco Carrara"}


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
    # instead of parsing a 400 error. The real registry is the four coded sources.
    code, body = server.dispatch("GET", ["adapters"], None)
    assert code == 200
    assert set(body["adapters"]) == {"marenostone", "polonine", "varsha", "zucchi"}
    assert server.dispatch("POST", ["adapters"], {})[0] == 405


# -- mint seed colour (seed a colourless new variety with a real colour, not 'Natural') --------

def test_get_colors_returns_the_medusa_vocab():
    code, body = server.dispatch("GET", ["colors"], None)
    assert code == 200
    assert "Beige" in body["colors"] and body["colors"] == sorted(body["colors"])   # canonical, sorted


def test_mint_with_a_seed_colour(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "color": "beige"})
    assert code == 200 and body["seed_color"] == "Beige"        # stored in canonical casing
    assert decisions.load_variety_seed_colors() == {"zucchi blue x": "Beige"}   # produce reads this
    row = server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]
    assert row["current_seed_color"] == "Beige"                 # UI reflects the between-runs choice


def test_mint_seed_colour_must_be_a_real_attribute(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "color": "definitely-not-a-colour"})
    assert code == 400 and "not a known" in body["error"]
    assert decisions.load_variety_seed_colors() == {}          # nothing seeded on a bad colour


def test_reject_ignores_a_seed_colour(seeded_queue):
    server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], {"action": "reject", "color": "beige"})
    assert decisions.load_variety_seed_colors() == {}          # only mint carries a seed colour


# -- mint seed type (assign a stone type to a type-less new variety; HELD until it has one) --------

def test_get_types_returns_the_medusa_vocab():
    code, body = server.dispatch("GET", ["types"], None)
    assert code == 200
    assert "Granite" in body["types"] and body["types"] == sorted(body["types"])   # canonical, sorted


def test_mint_with_a_seed_type(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "type": "granite"})
    assert code == 200 and body["seed_type"] == "Granite"       # stored in canonical casing
    assert decisions.load_variety_seed_types() == {"zucchi blue x": "Granite"}   # produce reads this
    row = server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]
    assert row["current_seed_type"] == "Granite"                # UI reflects the between-runs choice


def test_mint_seed_type_must_be_a_real_attribute(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "type": "definitely-not-a-type"})
    assert code == 400 and "not a known" in body["error"]
    assert decisions.load_variety_seed_types() == {}           # nothing seeded on a bad type


def test_reject_ignores_a_seed_type(seeded_queue):
    server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"], {"action": "reject", "type": "granite"})
    assert decisions.load_variety_seed_types() == {}           # only mint carries a seed type


# -- mint seed country (Q2: the operator picks the ORIGIN at approval; it overlays origin_map) --------

def test_mint_with_a_seed_country(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "country": "brazil"})
    assert code == 200 and body["seed_country"] == "BR"        # name resolved + stored as ISO2
    assert decisions_store.variety_seed_countries() == {"zucchi blue x": "BR"}   # load_all overlays this
    row = server.dispatch("GET", ["review", "variants"], None)[1]["variants"][0]
    assert row["current_seed_country"] == "BR"                 # UI reflects the between-runs choice


def test_mint_accepts_a_bare_iso_country(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "country": "no"})   # bare ISO2 -> canonical upper
    assert code == 200 and body["seed_country"] == "NO"
    assert decisions_store.variety_seed_countries() == {"zucchi blue x": "NO"}


def test_mint_seed_country_must_be_a_real_country(seeded_queue):
    code, body = server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                                 {"action": "mint", "country": "Xanadu"})
    assert code == 400 and "not a real" in body["error"]
    assert decisions_store.variety_seed_countries() == {}      # nothing seeded on a bogus country


def test_reject_ignores_a_seed_country(seeded_queue):
    server.dispatch("PUT", ["review", "variants", "Zucchi Blue X"],
                    {"action": "reject", "country": "brazil"})
    assert decisions_store.variety_seed_countries() == {}      # only mint carries a seed country


def test_origin_suggestion_endpoint():
    # the :4200 mint dialog pre-fills the country from the suggestion (exact map OR name pattern) -- a hint
    code, body = server.dispatch("GET", ["review", "variants", "Verde Persa", "origin-suggestion"], None)
    assert code == 200
    assert body["suggested_country"] == "BR"                   # 'persa' pattern -> BR (suggestion only)
