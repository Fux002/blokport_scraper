"""build.py argument scoping: `--sources` (which scrapers) and `--stage` (how far). The stage gate
must run ONLY the requested phases -- a catalog-only run never scrapes, a scrape-only run never
catalogs -- so the produce trigger can update one scraper without touching the rest."""

from __future__ import annotations

import pytest

from stone_pipeline import build


def test_parse_scope_defaults():
    assert build.parse_scope([]) == (None, "all")


def test_parse_scope_reads_sources_and_stage():
    assert build.parse_scope(["--sources", "zucchi,polonine", "--stage", "scrape"]) == \
        (["zucchi", "polonine"], "scrape")
    assert build.parse_scope(["--sources=zucchi", "--stage=catalog"]) == (["zucchi"], "catalog")
    assert build.parse_scope(["--sources", " zucchi , , polonine "]) == (["zucchi", "polonine"], "all")


def _stub_stages(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_scrape", lambda sources: calls.append(("scrape", sources)) or 0)
    monkeypatch.setattr(build, "_catalog", lambda: calls.append(("catalog", None)) or 0)
    monkeypatch.setattr(build, "_inventory", lambda sources: calls.append(("inventory", sources)) or 0)
    return calls


def test_stage_all_runs_scrape_then_catalog(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--sources", "zucchi"]) == 0
    assert calls == [("scrape", ["zucchi"]), ("catalog", None)]


def test_stage_scrape_only_never_catalogs(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--stage", "scrape", "--sources", "zucchi"]) == 0
    assert calls == [("scrape", ["zucchi"])]


def test_stage_catalog_only_never_scrapes(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--stage", "catalog"]) == 0
    assert calls == [("catalog", None)]


def test_stage_inventory_only_is_standalone(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--stage", "inventory", "--sources", "zucchi"]) == 0
    assert calls == [("inventory", ["zucchi"])]          # no scrape, no catalog


def test_stage_inventory_all_sources(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--stage", "inventory"]) == 0
    assert calls == [("inventory", None)]


def test_unknown_stage_exits_nonzero_and_runs_nothing(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.main(["--stage", "bogus"]) == 2
    assert calls == []


def test_a_failing_scrape_aborts_before_catalog(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_scrape", lambda sources: calls.append("scrape") or 1)
    monkeypatch.setattr(build, "_catalog", lambda: calls.append("catalog") or 0)
    assert build.main(["--sources", "zucchi"]) == 1
    assert calls == ["scrape"]           # catalog never runs on a failed scrape
