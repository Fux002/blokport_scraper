"""The shared name-key vectors: the scraper's match_key and the backend's nameKey must agree on
every case. The fixture is a PAIRED file, byte-identical to the backend's
src/model/catalog/__tests__/name-key.vectors.json; a change to either normaliser is a paired PR."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from stone_pipeline.core import text as text_mod
from stone_pipeline.core.text import UnfoldableName, clean_identifier, clean_name, match_key

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "name_key_vectors.json").read_text(encoding="utf-8"))


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def test_runtime_unicode_is_at_least_the_vector_floor():
    floor = _VECTORS["generator"]["floor"]
    assert _version(unicodedata.unidata_version) >= _version(floor), (
        f"runtime Unicode {unicodedata.unidata_version} is below the vectors' floor {floor}")


def test_match_key_agrees_with_every_shared_vector():
    wrong = [(c["input"], c["expected"], match_key(c["input"]))
             for c in _VECTORS["cases"] if match_key(c["input"]) != c["expected"]]
    assert not wrong, f"{len(wrong)} vectors disagree, first: {wrong[:3]}"


def _cleaned(clean, text: str):
    try:
        return clean(text)
    except UnfoldableName:
        return None


def test_both_display_rules_agree_with_every_shared_vector():
    wrong = [(c["input"], field, c[field], _cleaned(clean, c["input"]))
             for c in _VECTORS["cases"]
             for field, clean in (("display", clean_name), ("identifier", clean_identifier))
             if _cleaned(clean, c["input"]) != c[field]]
    assert not wrong, f"{len(wrong)} display vectors disagree, first: {wrong[:3]}"


def _alias(text: str):
    try:
        return text_mod.clean_alias(text) or None
    except getattr(text_mod, "AliasRefused", ValueError):
        return None


def test_the_alias_rule_agrees_with_every_shared_vector():
    assert hasattr(text_mod, "clean_alias"), "core.text has no clean_alias (the alias kind)"
    wrong = [(c["input"], c["alias"], _alias(c["input"]))
             for c in _VECTORS["cases"] if _alias(c["input"]) != c["alias"]]
    assert not wrong, f"{len(wrong)} alias vectors disagree, first: {wrong[:3]}"


def test_the_alias_key_agrees_with_every_shared_vector():
    assert hasattr(text_mod, "alias_key"), "core.text has no alias_key (the paired alias key)"
    wrong = [(c["input"], c["alias_key"], text_mod.alias_key(c["input"]))
             for c in _VECTORS["cases"] if text_mod.alias_key(c["input"]) != c["alias_key"]]
    assert not wrong, f"{len(wrong)} alias_key vectors disagree, first: {wrong[:3]}"
