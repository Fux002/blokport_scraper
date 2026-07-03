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


def test_bogus_source_alongside_a_real_one_is_rejected():
    # one unknown source in the request is a 400 -- never silently dropped (that would skip a scraper
    # the operator asked for, with no signal).
    rec, code = runner.start_run(sources=["zucchi", "not_a_scraper"], stage="scrape", launch=lambda r: None)
    assert code == 400 and "not_a_scraper" in str(rec["error"])
    assert runner._current_id is None                          # no run claimed


def test_catalog_stage_ignores_a_source_scope():
    # catalog is a SHARED all-source consolidation: a source scope is dropped so the command + the
    # 'last run' label don't credit one source for work that touched every source.
    seen, launch = _capture()
    rec, code = runner.start_run(sources=["zucchi"], stage="catalog", launch=launch)
    assert code == 202 and seen["rec"]["scope"] is None        # scope ignored for catalog
    assert "--sources" not in runner._build_command(seen["rec"])


def test_run_refused_while_a_reset_is_active(monkeypatch):
    monkeypatch.setattr(runner, "_reset_active", True)
    rec, code = runner.start_run(launch=lambda r: None)
    assert code == 409 and "reset is in progress" in rec["error"]


def test_dispatch_passes_sources_and_stage_through(monkeypatch):
    # the button posts {sources, stage}; dispatch must thread both into the run record. Stub the
    # launcher so nothing actually scrapes (the local launcher would Popen a build subprocess).
    monkeypatch.setattr(runner, "_LAUNCHERS",
                        {"local": lambda rec: rec.update(status="succeeded"),
                         "ecs": lambda rec: rec.update(status="succeeded")})
    code, body = dispatch("POST", ["run"], {"sources": ["polonine"], "stage": "scrape"})
    assert code == 202 and body["stage"] == "scrape" and body["sources"] == ["polonine"]


def test_run_record_exposes_scope_for_the_ui():
    _, launch = _capture()
    scoped, _ = runner.start_run(sources=["zucchi"], stage="scrape", launch=launch)
    assert scoped["scope"] == ["zucchi"]         # UI can spin ONLY this row (not every button)
    _, launch = _capture()
    all_run, _ = runner.start_run(launch=launch)  # prior run finished (_capture marks succeeded)
    assert all_run["scope"] is None              # an all-run -> UI spins "Run all"


def test_start_run_stamps_the_source_as_running(tmp_path, monkeypatch):
    # the durable 'last run' label: triggering a run marks its source running immediately, in config.db.
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=_min_yaml(tmp_path))
    _, launch = _capture()
    runner.start_run(sources=["polonine"], stage="scrape", launch=launch)
    row = store.get_row("polonine")
    assert row["last_run_status"] == "running" and row["last_run_stage"] == "scrape"
    assert row["last_run_at"] is not None


def _min_yaml(tmp_path):
    p = tmp_path / "sources.yaml"
    p.write_text("polonine:\n  adapter: polonine\n  source_code: pol\n  vendor: Polonine Stone Co\n",
                 encoding="utf-8")
    return p


def test_reset_refused_while_a_run_is_active():
    # coordination guard: never reset the ledger mid-run.
    runner._runs["x"] = {"status": "running", "run_id": "x"}
    runner._current_id = "x"
    body, code = runner.reset()
    assert code == 409 and "run is in progress" in body["error"]


def test_reset_bad_source_is_400():
    body, code = runner.reset(sources=["not_a_scraper"])
    assert code == 400


def test_reset_soft_and_hard_through_the_ledger(tmp_path, monkeypatch):
    # end to end against an ISOLATED ledger: soft re-serves (keeps rows), hard drops scraper output.
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, now_iso
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        now = now_iso()
        lg.upsert("variation", {"key": "slab_v1", "branch": "slab", "type": "Marble", "name": "x",
                                "aliases": "[]", "image_url": "u", "image_sha256": None, "image_model": None,
                                "volume": "", "medusa_id": "VID", "in_full": 1, "payload_hash": "",
                                "state": "synced", "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))
        lg.upsert("product", {"sku": "POL-1", "source": "pol", "variation_key": "slab_v1", "medusa_id": "PID",
                              "state": "synced", "created_at": now, "updated_at": now}, pk=("sku",))

    body, code = runner.reset()                       # soft
    assert code == 200 and body["mode"] == "soft" and body["reset"]["variation"] >= 1
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        assert lg.get("product", "sku", "POL-1")["state"] == "pending"   # kept, re-served
        assert lg.get("variation", "key", "slab_v1")["medusa_id"] is None

    body, code = runner.reset(hard=True)              # hard
    assert code == 200 and body["mode"] == "hard"
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        assert lg.get("product", "sku", "POL-1") is None                 # scraper output dropped
        assert lg.get("variation", "key", "slab_v1") is not None         # base config kept


def test_dispatch_routes_reset_with_hard_and_sources(monkeypatch):
    from stone_pipeline.config.server import dispatch
    seen = {}
    monkeypatch.setattr(runner, "reset",
                        lambda sources=None, hard=False: seen.update(sources=sources, hard=hard) or ({"ok": 1}, 200))
    code, body = dispatch("POST", ["reset"], {"hard": True, "sources": ["zucchi"]})
    assert code == 200 and seen == {"sources": ["zucchi"], "hard": True}


def test_current_returns_current_and_last_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    out = runner.current()
    assert set(out) == {"current", "last"} and out["current"] is None and out["last"] is None  # idle, none yet

    from stone_pipeline.config import store
    store.record_run_log({"run_id": "r1", "status": "succeeded", "stage": "all",
                          "sources": ["polonine"], "finished_at": "2026-07-03T10:00:00+00:00",
                          "counts": {"variation": 3, "product": 1, "inventory": 1}})
    runner._runs.clear(); runner._current_id = None          # simulate a config-server RESTART
    out = runner.current()
    assert out["current"] is None                            # nothing in flight after restart
    assert out["last"]["run_id"] == "r1"                     # ...but `last` survives, from the store
    assert out["last"]["counts"] == {"variation": 3, "product": 1, "inventory": 1}


def test_capture_counts_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    assert runner._capture_counts() == {"variation": 0, "product": 0, "inventory": 0}   # fresh ledger


def test_public_record_carries_counts_field():
    seen, launch = _capture()
    rec, _ = runner.start_run(stage="inventory", launch=launch)
    assert "counts" in rec and "stage" in rec and "sources" in rec   # the fields Medusa reads


def test_second_trigger_while_running_is_refused_409():
    # a launcher that leaves the run 'running' (does not finish it)
    rec1, code1 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code1 == 202
    rec2, code2 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code2 == 409 and rec2["run_id"] == rec1["run_id"]
