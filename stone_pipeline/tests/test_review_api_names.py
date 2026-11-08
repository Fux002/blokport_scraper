"""The review and settings API refuse a name that is not in its clean form (plan W12 to W14, W25): one rule with
the backend's refusal. 400 for no ASCII form, 400 with the clean form for anything else, 503 while the field kinds
are not published."""

from __future__ import annotations

import pytest

from stone_pipeline.config.control import server
from stone_pipeline.reference.loaders import field_kinds


def _decide(name):
    return server._put_review_decide({}, {"source": "polonine", "scraped": "x", "name": name, "type": "Granite"}, {})


@pytest.mark.parametrize("name, clean", [("Black & Gold", "Black and Gold"), ("Crème", "Creme")])
def test_a_stated_name_not_in_its_clean_form_is_refused_with_the_clean_form(name, clean):
    status, body = _decide(name)
    assert status == 400 and body["clean"] == clean


def test_a_stated_name_with_no_ascii_form_is_refused():
    status, body = _decide("黑金砂")
    assert status == 400 and "no ASCII form" in body["error"]


def test_the_name_routes_answer_503_while_the_field_kinds_are_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(field_kinds, "_path", lambda: tmp_path / "absent.json")
    status, _ = _decide("Black and Gold")
    assert status == 503


def test_an_origin_edit_refuses_an_unclean_city_or_variety():
    status, body = server._put_origin({}, {"variety": "Granite X", "stone_type": "Granite", "countries": ["BR"],
                                           "city": "São Paulo"}, {})
    assert status == 400 and body["clean"] == "Sao Paulo"
    status, body = server._put_origin({}, {"variety": "São Gabriel", "stone_type": "Granite",
                                           "countries": ["BR"]}, {})
    assert status == 400 and body["clean"] == "Sao Gabriel"


def test_an_attribute_id_refuses_a_value_that_is_not_its_clean_form():
    status, body = server._put_review_attribute({"value": "Marble (translucent)"},
                                                {"kind": "type", "medusa_id": "t_1"}, {})
    assert status == 400 and body["clean"] == "Marble translucent"
    status, body = server._put_review_attribute({"value": "x"}, {"kind": "shape", "medusa_id": "t_1"}, {})
    assert status == 400


def test_a_source_collection_city_must_be_clean():
    from stone_pipeline import adapters
    name = sorted(adapters.REGISTRY)[0]
    err = server._validate_source_put(name, {"ports": ["Rotterdam"], "collection_city": "São Francisco"}, None)
    assert err and err[0] == 400 and err[1]["clean"] == "Sao Francisco"
