"""The produce trigger (:4200 -> POST /config/v1/run): per-source (`sources`) and per-stage
(`stage`) scoping, and the subprocess command it composes. No real scrape runs -- the launcher is
injected, so these are fast and touch nothing. Source resolution runs against the real adapter
registry (marenostone/polonine/varsha/zucchi); the config store is isolated by conftest."""

from __future__ import annotations

import sys

import pytest

from stone_pipeline.config import runner
from stone_pipeline.config.server import dispatch


@pytest.fixture(autouse=True)
def _reset_runner():
    # runner state is process-global (one control plane); reset it around every test.
    runner._runs.clear()
    runner._current_id = None
    yield
    runner._runs.clear()
    runner._current_id = None


def _capture():
    """A launcher that records the rec it was handed and marks the run finished, so the single-run
    guard frees up for the next start_run in the same test."""
    seen: dict = {}

    def launch(rec):
        seen["rec"] = rec
        rec["status"] = "succeeded"
        rec["finished_at"] = "done"

    return seen, launch


def test_default_run_is_all_sources_full_stage():
    seen, launch = _capture()
    rec, code = runner.start_run(launch=launch)
    assert code == 202 and rec["stage"] == "all"
    assert seen["rec"]["scope"] is None                       # no subset -> build runs every enabled source
    assert runner._build_command(seen["rec"]) == [
        sys.executable, "-m", "stone_pipeline.build", "--stage", "all"]   # no --sources


def test_single_source_scrape_scopes_the_command():
    seen, launch = _capture()
    rec, code = runner.start_run(sources=["zucchi"], stage="scrape", launch=launch)
    assert code == 202 and rec["stage"] == "scrape" and rec["sources"] == ["zucchi"]
    assert runner._build_command(seen["rec"])[-4:] == ["--stage", "scrape", "--sources", "zucchi"]


def test_catalog_stage_carries_no_sources():
    seen, launch = _capture()
    rec, code = runner.start_run(stage="catalog", launch=launch)
    assert code == 202 and rec["stage"] == "catalog"
    assert "--sources" not in runner._build_command(seen["rec"])


def test_inventory_stage_scopes_to_one_source():
    seen, launch = _capture()
    rec, code = runner.start_run(sources=["zucchi"], stage="inventory", launch=launch)
    assert code == 202 and rec["stage"] == "inventory"
    assert runner._build_command(seen["rec"])[-4:] == ["--stage", "inventory", "--sources", "zucchi"]


def test_unknown_stage_is_rejected():
    rec, code = runner.start_run(stage="bogus", launch=lambda r: None)
    assert code == 400 and "bogus" in rec["error"]


def test_a_subset_with_no_known_source_is_rejected_not_run_all():
    rec, code = runner.start_run(sources=["not_a_scraper"], launch=lambda r: None)
    assert code == 400                                          # never a silent fall-through to all
    assert runner._current_id is None                          # no run was claimed


def test_bogus_source_alongside_a_real_one_is_dropped():
    seen, launch = _capture()
    rec, code = runner.start_run(sources=["zucchi", "not_a_scraper"], stage="scrape", launch=launch)
    assert code == 202 and rec["sources"] == ["zucchi"]        # only the known one survives
    assert seen["rec"]["scope"] == ["zucchi"]                  # and only it reaches the subprocess


def test_dispatch_passes_sources_and_stage_through(monkeypatch):
    # the button posts {sources, stage}; dispatch must thread both into the run record. Stub the
    # launcher so nothing actually scrapes (the local launcher would Popen a build subprocess).
    monkeypatch.setattr(runner, "_LAUNCHERS",
                        {"local": lambda rec: rec.update(status="succeeded"),
                         "ecs": lambda rec: rec.update(status="succeeded")})
    code, body = dispatch("POST", ["run"], {"sources": ["polonine"], "stage": "scrape"})
    assert code == 202 and body["stage"] == "scrape" and body["sources"] == ["polonine"]


def test_second_trigger_while_running_is_refused_409():
    # a launcher that leaves the run 'running' (does not finish it)
    rec1, code1 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code1 == 202
    rec2, code2 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code2 == 409 and rec2["run_id"] == rec1["run_id"]
