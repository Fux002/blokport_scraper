"""The paired variety-key vectors: the scraper's new_key/varietyKeyProblem/varietyKeyType and the backend's
variety-key.ts must agree on every case. The fixture is a PAIRED file, byte-identical to the backend's
src/model/catalog/variety-key.vectors.json; a change to the Key rule on either side is a paired PR."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stone_pipeline.phase1.variety_key import new_key, variety_key_problem, variety_key_type
from stone_pipeline.tests import gen_variety_key_vectors

_PATH = Path(__file__).parent / "fixtures" / "variety_key_vectors.json"
_VECTORS = json.loads(_PATH.read_text(encoding="utf-8"))


def test_fixture_is_the_current_generator_output():
    # The committed fixture equals a fresh build byte for byte, so a Key-rule change that forgets to
    # regenerate (and re-pair with the backend) fails here.
    rebuilt = {"_header": _VECTORS["_header"], "generator": _VECTORS["generator"],
               "cases": gen_variety_key_vectors.build()}
    assert json.dumps(rebuilt, ensure_ascii=True, indent=2, sort_keys=True) == \
        json.dumps(_VECTORS, ensure_ascii=True, indent=2, sort_keys=True)


def test_mint_cases_are_real_slug_keys():
    for c in (c for c in _VECTORS["cases"] if c["group"] == "mint"):
        assert new_key(c["type"], c["name"]) == c["key"], c["key"]


def test_problem_agrees_with_every_vector():
    wrong = [(c["key"], c["problem"], variety_key_problem(c["key"]))
             for c in _VECTORS["cases"] if variety_key_problem(c["key"]) != c["problem"]]
    assert not wrong, f"{len(wrong)} problem vectors disagree, first: {wrong[:3]}"


def test_type_parse_agrees_and_refused_keys_raise():
    for c in _VECTORS["cases"]:
        if c["problem"] is None:
            assert variety_key_type(c["key"]) == c["parsed_type"], c["key"]
        else:
            with pytest.raises(ValueError):
                variety_key_type(c["key"])


def test_every_group_is_covered():
    groups = {c["group"] for c in _VECTORS["cases"]}
    assert groups == {"mint", "parse", "refuse"}
