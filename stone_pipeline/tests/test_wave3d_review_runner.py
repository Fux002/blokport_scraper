"""Wave 3d: three silent outcomes in the control plane.

A malformed integer on the admin source PUT reached int() and answered 500; and two pending varieties with the
same name but different stone types were collapsed into one review card (a decision on the card would type
the second vendor's listing as the first's).
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, server






@pytest.mark.parametrize("field", ["default_bundle_size", "min_expected_rows"])
def test_a_malformed_integer_on_the_source_put_is_a_400_naming_the_field(field):
    body = {"source_code": "POL", "ports": ["port_1"], field: "abc"}
    result = server._validate_source_put("polonine", body)
    assert result is not None
    status, err = result
    assert status == 400 and field in err["error"]


@pytest.mark.xfail(strict=True, reason="reproduced: variety cards are keyed by norm(name) only, so two types "
                   "collapse into one card; the ref shape is load-bearing for the reject PUT and Blokport's "
                   "review UI, so the fix is the review-keying item of the structure wave")
def test_two_pending_varieties_with_the_same_name_and_different_types_are_two_cards():
    from stone_pipeline.stages import decisions as stage_decisions
    pending = [{"variant": "Imperial White", "reason": "r", "stone_type": "Granite", "src": "marenostone",
                "scraped": "Imperial White Granite", "sources": ["marenostone"]},
               {"variant": "Imperial White", "reason": "r", "stone_type": "Quartzite", "src": "zucchi",
                "scraped": "Imperial White Quartzite", "sources": ["zucchi"]}]
    assert stage_decisions.write_confirm_file(pending) == 2
    cards = decisions_store.list_pending("variety")
    assert sorted(c["stone_type"] for c in cards) == ["Granite", "Quartzite"]
