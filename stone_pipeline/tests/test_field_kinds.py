"""field-kinds.json: the backend's ONE map of which name field is a NAME and which an IDENTIFIER (PHASE-0 N16).

The scraper is its one reader: there is no default map and no default kind, a failure is never cached, and a map
that arrives after boot is used without a restart (plan W1)."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

from stone_pipeline.reference.loaders import field_kinds as fk

FIXTURE = Path(__file__).parent / "fixtures" / "field-kinds.json"


@pytest.fixture
def map_path(tmp_path, monkeypatch):
    path = tmp_path / "field-kinds.json"
    monkeypatch.setattr(fk, "_path", lambda: path)
    monkeypatch.setattr(fk, "_cache", None)
    return path


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")


def _fixture_doc():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_lists_every_required_field_with_a_known_kind():
    doc = _fixture_doc()
    assert set(doc["fields"]) == fk.REQUIRED
    assert set(doc["fields"].values()) <= {fk.NAME, fk.IDENTIFIER}


@pytest.mark.parametrize("breakage, reason", [
    (None, "absent"),
    ("not json", "not valid JSON"),
    ({"fields": {}}, "brand"),
    ({"brand": "wudport", "fields": {}}, "brand"),
    ("drop", "missing"),
    ("bad kind", "kind"),
])
def test_a_broken_map_is_a_load_error_never_a_default(map_path, breakage, reason):
    if breakage == "not json":
        map_path.write_text("{", encoding="utf-8")
    elif breakage == "drop":
        doc = _fixture_doc(); doc["fields"].pop("variation.name"); _write(map_path, doc)
    elif breakage == "bad kind":
        doc = _fixture_doc(); doc["fields"]["quality.name"] = "code"; _write(map_path, doc)
    elif breakage is not None:
        _write(map_path, breakage)
    with pytest.raises(fk.FieldKindsError, match=reason):
        fk.kinds()


def test_a_failure_is_never_cached_and_a_late_map_is_used_without_a_restart(map_path):
    with pytest.raises(fk.FieldKindsError):
        fk.kinds()
    shutil.copy(FIXTURE, map_path)
    assert fk.kinds()["quality.name"] == fk.IDENTIFIER
    doc = _fixture_doc(); doc["fields"]["quality.name"] = fk.NAME; _write(map_path, doc)
    assert fk.kinds()["quality.name"] == fk.NAME          # a changed file is read again


def test_clean_and_display_follow_the_field_kind(map_path):
    shutil.copy(FIXTURE, map_path)
    assert fk.display(fk.QUALITY_NAME, "AA/BB") == "AA/BB"                 # identifier: punctuation and case kept
    assert fk.display(fk.QUALITY_NAME, "PINK") == "PINK"
    assert fk.display(fk.VARIATION_NAME, "ROSA porriño") == "Rosa Porrino"   # name: cleaned, title-cased
    assert fk.clean(fk.VARIATION_ALIAS_VALUE, "Black & Gold") == "Black and Gold"
    assert fk.clean(fk.PRODUCT_TITLE, "Black & Gold Polished") == "Black & Gold Polished"


def test_an_unknown_field_fails_at_once(map_path):
    shutil.copy(FIXTURE, map_path)
    with pytest.raises(KeyError):
        fk.clean("variation.nam", "x")


class _FakeS3:
    def __init__(self, body):
        self._body = body

    def download_file(self, bucket, key, filename):
        if self._body is None:
            raise RuntimeError("NoSuchKey")
        Path(filename).write_text(self._body, encoding="utf-8")


def test_fetch_writes_a_valid_map_and_raises_on_any_failure(tmp_path):
    from deploy import fetch_inputs
    target = tmp_path / "field-kinds.json"
    fetch_inputs.fetch_field_kinds(client=_FakeS3(FIXTURE.read_text(encoding="utf-8")), target=target)
    assert json.loads(target.read_text(encoding="utf-8"))["brand"] == "blokport"
    before = target.read_bytes()
    for body in (None, "{", json.dumps({"brand": "blokport", "fields": {}})):
        with pytest.raises(Exception):
            fetch_inputs.fetch_field_kinds(client=_FakeS3(body), target=target)
        assert target.read_bytes() == before                 # a failed fetch never replaces the good file
    assert not list(tmp_path.glob("*.incoming"))


def test_a_produce_stops_when_the_map_cannot_be_fetched(monkeypatch):
    from deploy import fetch_inputs
    from stone_pipeline import produce
    monkeypatch.setattr(fetch_inputs, "main", lambda: 0)
    monkeypatch.setattr(fetch_inputs, "fetch_field_kinds", lambda: (_ for _ in ()).throw(RuntimeError("absent")))
    with pytest.raises(SystemExit) as stop:
        produce._fetch_inputs()
    assert stop.value.code == 2


def test_no_module_but_the_loader_and_text_imports_the_cleaners():
    # No second map in code: every writer cleans through field_kinds, which reads the backend's map.
    root = Path(fk.__file__).resolve().parents[2]
    allowed = {root / "reference" / "loaders" / "field_kinds.py", root / "core" / "text.py"}
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.relative_to(root).parts or path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            if {"clean_name", "clean_identifier"} & set(names):
                offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders
