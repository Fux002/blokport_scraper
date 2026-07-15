"""The produce trigger (:4200 -> POST /config/v1/run): per-source (`sources`) and per-stage
(`stage`) scoping, and the subprocess command it composes. No real scrape runs -- the launcher is
injected, so these are fast and touch nothing. Source resolution runs against the real adapter
registry (marenostone/polonine/varsha/zucchi); the config store is isolated by conftest."""

from __future__ import annotations

import sys

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.config import runner
from stone_pipeline.config.server import dispatch


@pytest.fixture(autouse=True)
def _reset_runner():
    # runner + lifecycle state is process-global (one control plane); reset it around every test.
    runner._runs.clear()
    runner._current_id = None
    lifecycle._active = None
    yield
    runner._runs.clear()
    runner._current_id = None
    lifecycle._active = None


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
    assert seen["rec"]["scope"] is None                       # no subset -> produce runs every enabled source
    assert runner._build_command(seen["rec"]) == [
        sys.executable, "-m", "stone_pipeline.produce", "--stage", "all"]   # full produce (fetch -> scrape -> build)


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


def test_publish_deliverables_only_on_catalog_producing_stages(monkeypatch):
    # A catalog/republish/all produce regenerates to_upload/, so its deliverables (the valid-combination
    # files) are published to S3; an inventory/scrape produce does not touch them, so it must not re-push
    # the unchanged ~334MB set. Best-effort: a publish failure never raises out of the finish path.
    from deploy import upload_artifacts
    calls: list[str] = []
    monkeypatch.setattr(upload_artifacts, "main", lambda: calls.append("published") or 0)

    for stage in ("all", "catalog", "republish"):
        runner._publish_deliverables(stage)
    assert calls == ["published", "published", "published"]     # every catalog-producing stage publishes

    calls.clear()
    for stage in ("scrape", "inventory"):
        runner._publish_deliverables(stage)
    assert calls == []                                          # no deliverable change -> no publish


def test_publish_deliverables_swallows_a_publish_error(monkeypatch):
    # the fixed keys going stale is a soft failure (operator can re-run); it must never fail an
    # otherwise-good produce, so a publish exception is logged, not raised.
    from deploy import upload_artifacts

    def _boom():
        raise RuntimeError("s3 down")
    monkeypatch.setattr(upload_artifacts, "main", _boom)
    runner._publish_deliverables("catalog")                     # does not raise


def test_run_refused_while_a_lifecycle_op_is_active(monkeypatch):
    monkeypatch.setattr(lifecycle, "_active", "reset")     # a destructive op holds the slot
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
    body, code = lifecycle.reset()
    assert code == 409 and "run is in progress" in body["error"]


def test_soft_reset_preserves_a_retiring_variety(tmp_path, monkeypatch):
    # F6: a global soft reset must keep a 'retiring' variety fully intact -- state AND medusa_id (which is
    # load-bearing: it says "Medusa has this, delete it"). NULLing it would corrupt the retire/ack machine.
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, now_iso
    from stone_pipeline.ledger.sync import retire_variation
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        now = now_iso()
        lg.upsert("variation", {"key": "K", "branch": "slab", "type": "Marble", "name": "X", "aliases": "[]",
                                "image_url": "u", "image_sha256": None, "image_model": None, "volume": "",
                                "medusa_id": "VID", "in_full": 0, "payload_hash": "", "state": "synced",
                                "first_seen": now, "last_synced": now, "created_at": now, "updated_at": now},
                  pk=("key",))
        retire_variation(lg, "K")
        assert lg.get("variation", "key", "K")["state"] == "retiring"
    body, code = lifecycle.reset()                     # global soft reset
    assert code == 200
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        row = lg.get("variation", "key", "K")
        assert row["state"] == "retiring" and row["medusa_id"] == "VID"   # both preserved


def test_reset_unknown_source_is_404():
    # ISS-5: lifecycle ops resolve identity via the config store, so an unknown source is a uniform 404
    # (was 400 when reset keyed on the adapter registry).
    body, code = lifecycle.reset(sources=["not_a_scraper"])
    assert code == 404 and "not_a_scraper" in str(body["error"])


def test_lifecycle_ops_address_a_config_only_source(tmp_path, monkeypatch):
    # ISS-5: reset / delist / purge must resolve a config-only source (no coded adapter) the SAME way
    # remove_source does -- via the store -- instead of the old 400 "unknown source(s)" from the registry.
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    store.upsert_row({"source": "zzprobe", "adapter": "zzprobe", "source_code": "zzp", "vendor": "Junk"})
    for verb in (lambda: lifecycle.reset(sources=["zzprobe"]),
                 lambda: lifecycle.delist(["zzprobe"]),
                 lambda: lifecycle.purge(["zzprobe"])):
        _body, code = verb()
        assert code == 200, f"config-only source should resolve, got {code}: {_body}"


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

    body, code = lifecycle.reset()                       # soft
    assert code == 200 and body["mode"] == "soft" and body["reset"]["variation"] >= 1
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        assert lg.get("product", "sku", "POL-1")["state"] == "pending"   # kept, re-served
        assert lg.get("variation", "key", "slab_v1")["medusa_id"] is None

    body, code = lifecycle.reset(hard=True)              # hard
    assert code == 200 and body["mode"] == "hard"
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        assert lg.get("product", "sku", "POL-1") is None                 # scraper output dropped
        assert lg.get("variation", "key", "slab_v1") is not None         # base config kept


def test_dispatch_routes_reset_with_hard_and_sources(monkeypatch):
    from stone_pipeline.config.server import dispatch
    seen = {}
    monkeypatch.setattr(lifecycle, "reset",
                        lambda sources=None, hard=False, pristine=False:
                        seen.update(sources=sources, hard=hard, pristine=pristine) or ({"ok": 1}, 200))
    code, body = dispatch("POST", ["reset"], {"hard": True, "sources": ["zucchi"]})
    assert code == 200 and seen == {"sources": ["zucchi"], "hard": True, "pristine": False}
    # the factory-reset flag threads through as pristine=True (global, no sources)
    code, body = dispatch("POST", ["reset"], {"pristine": True})
    assert code == 200 and seen == {"sources": None, "hard": False, "pristine": True}


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


def test_clean_deletes_a_sources_scraped_data(tmp_path, monkeypatch):
    import types
    from stone_pipeline.config import store
    import stone_pipeline.clean as clean_mod
    # ISS-5: lifecycle ops resolve identity via the config store, so the source must exist there (as it
    # does in prod, seeded on boot) -- seed it rather than rely on the adapter registry.
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "zucchi", "adapter": "zucchi", "source_code": "zuc", "vendor": "Z"})
    monkeypatch.setattr(clean_mod, "SETTINGS",
                        types.SimpleNamespace(paths=types.SimpleNamespace(data_dir=tmp_path)))
    (tmp_path / "zucchi" / "20260625_222931").mkdir(parents=True)
    (tmp_path / "zucchi" / "20260601_090000").mkdir(parents=True)
    (tmp_path / "polonine" / "20260621_104529").mkdir(parents=True)
    body, code = lifecycle.clean(sources=["zucchi"])
    assert code == 200 and body["deleted_scrapes"] == {"zucchi": 2}
    assert list((tmp_path / "zucchi").glob("*")) == []          # zucchi scrapes gone
    assert (tmp_path / "polonine" / "20260621_104529").exists()  # polonine untouched


def test_clean_refused_while_a_run_is_active():
    runner._runs["x"] = {"status": "running", "run_id": "x"}
    runner._current_id = "x"
    body, code = lifecycle.clean(sources=["zucchi"])       # sources path -> guard returns before any fs work
    assert code == 409


def test_clean_unknown_source_is_404():
    body, code = lifecycle.clean(sources=["not_a_scraper"])
    assert code == 404


def test_purge_refused_while_a_run_is_active():
    runner._runs["x"] = {"status": "running", "run_id": "x"}
    runner._current_id = "x"
    body, code = lifecycle.purge()
    assert code == 409


def test_dispatch_routes_purge(monkeypatch):
    from stone_pipeline.config.server import dispatch
    seen = {}
    monkeypatch.setattr(lifecycle, "purge",
                        lambda sources=None: seen.update(sources=sources) or ({"external_ids": []}, 200))
    code, body = dispatch("POST", ["purge"], {"sources": ["polonine"]})
    assert code == 200 and seen == {"sources": ["polonine"]}


def test_dispatch_routes_clean(monkeypatch):
    from stone_pipeline.config.server import dispatch
    seen = {}
    monkeypatch.setattr(lifecycle, "clean", lambda sources=None: seen.update(sources=sources) or ({"ok": 1}, 200))
    code, body = dispatch("POST", ["clean"], {"sources": ["zucchi"]})
    assert code == 200 and seen == {"sources": ["zucchi"]}


# -- pause / resume: freeze a source WITHOUT taking its products offline --------------------------

def test_pause_freezes_a_source_config_only(tmp_path, monkeypatch):
    # pause is config-only: lifecycle=paused + enabled=0 (so run_all skips it), and it must NEVER open
    # the ledger (products stay live & buyable, untouched). Make Ledger.open explode to prove it.
    from stone_pipeline.config import store
    from stone_pipeline.ledger import db as ledger_db
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=_min_yaml(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("pause/resume must not open the ledger")
    monkeypatch.setattr(ledger_db.Ledger, "open", _boom)

    body, code = lifecycle.pause(["polonine"])
    assert code == 200 and body == {"paused": ["polonine"]}
    row = store.get_row("polonine")
    assert row["lifecycle"] == "paused" and row["enabled"] is False
    assert store.enabled_names() == set()                    # paused -> not scraped next run

    body, code = lifecycle.resume(["polonine"])
    assert code == 200 and body == {"resumed": ["polonine"], "restock_required": []}  # paused, not delisted
    row = store.get_row("polonine")
    assert row["lifecycle"] == "active" and row["enabled"] is True
    assert store.enabled_names() == {"polonine"}             # resumed -> scraped again


def test_pause_resume_reject_empty_or_unknown_scope():
    assert lifecycle.pause([])[1] == 400                    # empty scope -> 400 (needs an explicit list)
    assert lifecycle.pause(["not_a_scraper"])[1] == 404     # unknown source -> 404 (ISS-5, store identity)
    assert lifecycle.resume([])[1] == 400
    assert lifecycle.resume(["not_a_scraper"])[1] == 404


def test_delist_stamps_lifecycle_delisted(tmp_path, monkeypatch):
    # delist must set lifecycle=delisted (distinct from paused) so Medusa can tell offline from frozen.
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    store.seed_from_yaml(yaml_path=_min_yaml(tmp_path))
    body, code = lifecycle.delist(["polonine"])
    assert code == 200
    row = store.get_row("polonine")
    assert row["lifecycle"] == "delisted" and row["enabled"] is False


def test_clean_refused_on_a_paused_source(tmp_path, monkeypatch):
    # a paused source's raw scrape is the frozen snapshot the catalog rebuilds from; refuse to wipe it.
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=_min_yaml(tmp_path))
    lifecycle.pause(["polonine"])
    body, code = lifecycle.clean(sources=["polonine"])
    assert code == 409 and "polonine" in str(body["error"])


def test_dispatch_routes_pause_and_resume(monkeypatch):
    from stone_pipeline.config.server import dispatch
    seen = {}
    monkeypatch.setattr(lifecycle, "pause",
                        lambda sources=None: seen.update(pause=sources) or ({"paused": sources}, 200))
    monkeypatch.setattr(lifecycle, "resume",
                        lambda sources=None: seen.update(resume=sources) or ({"resumed": sources}, 200))
    code, body = dispatch("POST", ["pause"], {"sources": ["zucchi"]})
    assert code == 200 and seen["pause"] == ["zucchi"] and body == {"paused": ["zucchi"]}
    code, body = dispatch("POST", ["resume"], {"sources": ["zucchi"]})
    assert code == 200 and seen["resume"] == ["zucchi"]
    assert dispatch("GET", ["pause"], None)[0] == 405           # wrong method


def test_dispatch_routes_variation_retire_and_un_retire(monkeypatch):
    seen = {}
    monkeypatch.setattr(lifecycle, "retire_variation",
                        lambda key, force=False: seen.update(retire=(key, force)) or ({"retired": key}, 200))
    monkeypatch.setattr(lifecycle, "un_retire",
                        lambda key: seen.update(un_retire=key) or ({"un_retired": key}, 200))
    code, _ = dispatch("POST", ["variations", "slab_marble_x_1", "retire"], {"force": True})
    assert code == 200 and seen["retire"] == ("slab_marble_x_1", True)
    code, _ = dispatch("POST", ["variations", "slab_marble_x_1", "un_retire"], None)
    assert code == 200 and seen["un_retire"] == "slab_marble_x_1"
    assert dispatch("GET", ["variations", "x", "retire"], None)[0] == 404      # wrong method


def test_second_trigger_while_running_is_refused_409():
    # a launcher that leaves the run 'running' (does not finish it)
    rec1, code1 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code1 == 202
    rec2, code2 = runner.start_run(launch=lambda r: r.update(status="running"))
    assert code2 == 409 and rec2["run_id"] == rec1["run_id"]


def test_remove_config_only_source_is_deletable(tmp_path, monkeypatch):
    # ISS-2: a source with a config row but NO coded adapter (added via PUT, or a stale row whose adapter
    # was removed) must be REMOVABLE. remove_source resolves its code from config.db, not the runnable
    # adapter registry, so it no longer 400s "unknown source" the way /run correctly does.
    from stone_pipeline.config import store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    store.upsert_row({"source": "zztest", "adapter": "zztest", "source_code": "zzt", "vendor": "Junk"})
    assert store.get_row("zztest") is not None
    body, code = lifecycle.remove_source("zztest")
    assert code == 200 and body["config_removed"] is True            # not 400 'unknown source'
    assert store.get_row("zztest") is None                           # config row gone


def test_remove_unknown_source_is_404(tmp_path, monkeypatch):
    from stone_pipeline.config import store  # noqa: F401  (ensures the isolated config db is used)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    body, code = lifecycle.remove_source("does_not_exist")
    assert code == 404
