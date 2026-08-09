"""A CONFLICTING duplicate attribute name (two values normalizing to the same key but mapping to DIFFERENT
Medusa ids) must fail loud in BOTH attribute loaders -- otherwise the value->id map silently last-wins and a
mint/combination ships the wrong id (the exact hazard flagged: names are unique per category today, so the
guard is a no-op now and surfaces a future Medusa duplicate instead). A same-id repeat is harmless."""

from __future__ import annotations

import pytest

from stone_pipeline.reference.loaders import load_attributes
from stone_pipeline.stages.tree_build import _load_attributes


def _write(tmp_path, rows):
    p = tmp_path / "attributes.csv"
    body = "category,value,sourceid\n" + "".join(f"{c},{v},{i}\n" for c, v, i in rows)
    p.write_text(body, encoding="utf-8")
    return p


def test_conflicting_duplicate_fails_loud_in_load_attributes(tmp_path):
    p = _write(tmp_path, [("color", "Amber", "id_a"), ("color", "amber", "id_b")])   # same norm, diff id
    with pytest.raises(ValueError, match="unique per category"):
        load_attributes(p)


def test_conflicting_duplicate_fails_loud_in_tree_build_loader(tmp_path):
    p = _write(tmp_path, [("color", "Amber", "id_a"), ("color", "amber", "id_b")])
    with pytest.raises(ValueError, match="unique per category"):
        _load_attributes(p)


def test_same_id_duplicate_is_allowed(tmp_path):
    p = _write(tmp_path, [("color", "Amber", "id_a"), ("color", "Amber", "id_a")])   # idempotent repeat
    assert load_attributes(p).canonical_names("color") == ["Amber"]
    assert _load_attributes(p)["color"] == {"amber": "id_a"}


def test_unique_names_load_normally(tmp_path):
    p = _write(tmp_path, [("color", "Amber", "id_a"), ("color", "Grey", "id_g")])
    assert set(load_attributes(p).canonical_names("color")) == {"Amber", "Grey"}
    assert _load_attributes(p)["color"] == {"amber": "id_a", "grey": "id_g"}
