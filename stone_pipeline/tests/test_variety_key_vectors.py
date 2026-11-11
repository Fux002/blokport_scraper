"""The paired variety-key vectors: the scraper's new_key / variety_key_problem / variety_key_type and the backend's
variety-key.ts must agree on every case. The file runs ONE way, scraper -> backend: this repo generates it
(gen_variety_key_vectors) in the backend's shape, and the backend keeps a byte-identical copy. A change to the Key rule
on either side is a paired PR."""
from __future__ import annotations

from pathlib import Path

import json
import pytest

from stone_pipeline.phase1.variety_key import new_key, variety_key_problem, variety_key_type
from stone_pipeline.tests import gen_variety_key_vectors

_PATH = Path(__file__).parent / "fixtures" / "variety_key_vectors.json"
_VECTORS = json.loads(_PATH.read_text(encoding="utf-8"))


def test_the_fixture_is_the_generator_s_output_byte_for_byte():
    # a Key-rule change that forgets to regenerate (and re-pair with the backend) fails here
    assert _PATH.read_text(encoding="utf-8") == gen_variety_key_vectors.render(gen_variety_key_vectors.build())


def test_the_fixture_is_in_the_backend_s_shape_and_covers_every_problem():
    assert set(_VECTORS) == {"_header", "valid", "refused"}
    assert all(set(c) == {"key", "type"} for c in _VECTORS["valid"])
    assert all(set(c) == {"key", "problem"} for c in _VECTORS["refused"])
    assert {c["problem"] for c in _VECTORS["refused"]} == {"no_type_separator", "underscore_run", "empty_type",
                                                          "empty_name", "second_type_separator"}
    assert {"dolomite", "dolomite_marble"} <= {c["type"] for c in _VECTORS["valid"]}   # the prefix pair


@pytest.mark.parametrize("case", _VECTORS["valid"], ids=lambda c: c["key"])
def test_every_valid_vector_passes_and_parses_to_its_type(case):
    assert variety_key_problem(case["key"]) is None
    assert variety_key_type(case["key"]) == case["type"]


@pytest.mark.parametrize("case", _VECTORS["refused"], ids=lambda c: c["key"] or "empty")
def test_every_refused_vector_reports_its_problem_and_never_parses(case):
    assert variety_key_problem(case["key"]) == case["problem"]
    with pytest.raises(ValueError, match=case["problem"]):
        variety_key_type(case["key"])


@pytest.mark.parametrize("type_name, name", [("Marble", ""), ("Marble", " - "), ("Marble", "!!"), ("", "Carrara"),
                                             ("!!", "Carrara")])
def test_a_type_or_name_that_slugs_to_nothing_mints_no_key(type_name, name):
    # without the refusal it would mint `marble___<uuid>` (an underscore run) or `__carrara_<uuid>` (no type)
    with pytest.raises(ValueError, match="slugs to nothing"):
        new_key(type_name, name)
