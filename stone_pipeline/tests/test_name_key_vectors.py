"""The shared name-key vectors: the scraper's match_key and the backend's nameKey must agree on
every case. The fixture is a PAIRED file, byte-identical to the backend's
src/model/catalog/__tests__/name-key.vectors.json; a change to either normaliser is a paired PR."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from stone_pipeline.core.text import UnfoldableName, clean_name, match_key

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


def _display(text: str):
    try:
        return clean_name(text)
    except UnfoldableName:
        return None


def test_clean_name_agrees_with_every_shared_display_vector():
    wrong = [(c["input"], c["display"], _display(c["input"]))
             for c in _VECTORS["cases"] if _display(c["input"]) != c["display"]]
    assert not wrong, f"{len(wrong)} display vectors disagree, first: {wrong[:3]}"
