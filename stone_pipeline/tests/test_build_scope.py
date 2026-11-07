"""build run scoping: sources (which scrapers) and stage (how far). The stage gate must run ONLY the
requested phases -- a catalog-only run never scrapes, a scrape-only run never catalogs -- so the produce
trigger can update one scraper without touching the rest. C12: config-driven (SETTINGS.run from the
SCRAPER_RUN_* env), no argv; build.run is the param-driven core, main() reads the scope once."""

from __future__ import annotations

import dataclasses

from stone_pipeline import build
from stone_pipeline.config import settings


def _stub_stages(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_scrape", lambda sources: calls.append(("scrape", sources)) or 0)
    monkeypatch.setattr(build, "_catalog", lambda: calls.append(("catalog", None)) or 0)
    monkeypatch.setattr(build, "_inventory", lambda sources: calls.append(("inventory", sources)) or 0)
    return calls


def test_stage_all_runs_scrape_then_catalog(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(["zucchi"], "all") == 0
    assert calls == [("scrape", ["zucchi"]), ("catalog", None)]


def test_stage_scrape_only_never_catalogs(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(["zucchi"], "scrape") == 0
    assert calls == [("scrape", ["zucchi"])]


def test_stage_catalog_only_never_scrapes(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(None, "catalog") == 0
    assert calls == [("catalog", None)]


def test_stage_inventory_only_is_standalone(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(["zucchi"], "inventory") == 0
    assert calls == [("inventory", ["zucchi"])]          # no scrape, no catalog


def test_stage_inventory_all_sources(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(None, "inventory") == 0
    assert calls == [("inventory", None)]


def test_unknown_stage_exits_nonzero_and_runs_nothing(monkeypatch):
    calls = _stub_stages(monkeypatch)
    assert build.run(None, "bogus") == 2                 # boundary validation, as the CLI did
    assert calls == []


def test_a_failing_scrape_aborts_before_catalog(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_scrape", lambda sources: calls.append("scrape") or 1)
    monkeypatch.setattr(build, "_catalog", lambda: calls.append("catalog") or 0)
    assert build.run(["zucchi"], "all") == 1
    assert calls == ["scrape"]           # catalog never runs on a failed scrape


# main() reads the scope ONCE from SETTINGS.run (the SCRAPER_RUN_* env); no argv.
def _with_run(monkeypatch, **kw):
    run_cfg = dataclasses.replace(settings.SETTINGS.run, **kw)
    monkeypatch.setattr(build, "SETTINGS", dataclasses.replace(settings.SETTINGS, run=run_cfg))


def test_main_reads_stage_and_sources_from_settings_run(monkeypatch):
    calls = _stub_stages(monkeypatch)
    _with_run(monkeypatch, stage="scrape", sources=("zucchi",), verify=False)
    assert build.main() == 0
    assert calls == [("scrape", ["zucchi"])]


def test_main_empty_sources_means_all(monkeypatch):
    calls = _stub_stages(monkeypatch)
    _with_run(monkeypatch, stage="inventory", sources=(), verify=False)
    assert build.main() == 0
    assert calls == [("inventory", None)]                # () -> None (all enabled)


def test_main_verify_short_circuits(monkeypatch):
    calls = _stub_stages(monkeypatch)
    monkeypatch.setattr(build, "verify_gate", lambda: 0)
    _with_run(monkeypatch, verify=True, stage="all")
    assert build.main() == 0
    assert calls == []                                   # verify re-checks only; no scrape/catalog
