"""The paired phase 2 vectors (P2-10 C0 and C1): the scraper's slug and its alias boundary agree with the backend on
every case.

Both fixtures are PAIRED files, byte-identical copies of the backend's:
- `fixtures/name_slug_vectors.json` = `src/model/catalog/__tests__/name-slug.vectors.json` at backend 30f2b6943
  (sha256 a9bd8c37b8113b0c14bfec06d384548ca39656af3c90336e5f99c34504f21d89), P2-3a, table 175 S1: the backend's
  nameSlug is the scraper's slugify step for step. Written ASCII-escaped, like the name-key vectors
  (gen_name_key_vectors.py), so no literal em dash reaches the repo's em-dash gate;
- `fixtures/alias_problem_vectors.json` = `src/model/catalog/__tests__/alias-problem.vectors.json` at backend 184e8a9e
  (`feat/phase2-alias`), P2-2, table 171: aliasProblem's seven checks in order, and codeMatcher over nameKey's tokens.
A change to either side is a paired PR. The key is core.text.match_key, the backend nameKey's paired twin
(test_name_key_vectors.py).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from stone_pipeline.config.domain import load_pack
from stone_pipeline.core.text import slugify

_FIXTURES = Path(__file__).parent / "fixtures"
_SLUG = json.loads((_FIXTURES / "name_slug_vectors.json").read_text(encoding="utf-8"))
_ALIAS = json.loads((_FIXTURES / "alias_problem_vectors.json").read_text(encoding="utf-8"))
# the pack each brand's stack runs (infra/modules/scraper/variables.tf:82: blokport = stone, wudport = wood)
_BRAND_PACK = {"blokport": "stone", "wudport": "wood"}


def _boundary():
    """The scraper's port, imported per test so a missing module fails each test by name, not the whole file."""
    return importlib.import_module("stone_pipeline.core.alias_problem")


def _context(raw: dict):
    return _boundary().AliasContext(own_name=raw["ownName"], type_words=frozenset(raw["typeWords"]),
                                    vocabulary_keys=frozenset(raw["vocabularyKeys"]),
                                    same_type_name_keys=frozenset(raw["sameTypeNameKeys"]))


def _pattern(case: dict):
    return case["pattern"] if "pattern" in case else _ALIAS["brands"][case["brand"]]


def test_slugify_agrees_with_every_shared_slug_vector():
    wrong = [(c["input"], c["expected"], slugify(c["input"])) for c in _SLUG["cases"]
             if slugify(c["input"]) != c["expected"]]
    assert not wrong, f"{len(wrong)} slug vectors disagree, first: {wrong[:3]}"


def test_alias_problem_agrees_with_every_shared_vector():
    boundary = _boundary()
    wrong = [(c["group"], c["expected"], boundary.alias_problem(c["value"], _context(c["context"])))
             for c in _ALIAS["alias_cases"]
             if boundary.alias_problem(c["value"], _context(c["context"])) != c["expected"]]
    assert not wrong, f"{len(wrong)} alias vectors disagree, first: {wrong[:3]}"


def test_alias_problem_ignores_case_and_accents():
    # the backend's spec runs every case upper-cased too: the answer never depends on the case
    boundary = _boundary()
    wrong = [c["group"] for c in _ALIAS["alias_cases"]
             if boundary.alias_problem(c["value"].upper(), _context(c["context"])) != c["expected"]]
    assert not wrong, f"upper-casing changed the answer: {wrong[:3]}"


def test_alias_problem_names_exactly_the_backend_problems_in_order():
    assert _boundary().PROBLEMS == ("delimiter", "empty", "own_name", "own_name_type_word", "same_type_name",
                                    "bare_vocabulary_word", "too_short")


def test_code_matcher_agrees_with_every_shared_vector():
    boundary = _boundary()
    wrong = [(c["group"], c["expected"]) for c in _ALIAS["code_cases"]
             if boundary.code_matcher(_pattern(c))(c["value"]) != c["expected"]]
    assert not wrong, f"{len(wrong)} code vectors disagree, first: {wrong[:3]}"


def test_each_brands_code_pattern_is_its_packs_pattern():
    # the vectors state each brand's pattern; the scraper's copy runs against the pack itself, so a drift fails here
    assert {brand: load_pack(pack).name_code_pattern for brand, pack in _BRAND_PACK.items()} == _ALIAS["brands"]
