"""The scraper config store: seed from sources.yaml, the pipeline reads it, and the
enabled flag controls which scrapers run."""

from __future__ import annotations

from stone_pipeline.config import store
from stone_pipeline.config.sources import load_source, load_sources


def _seed_yaml(tmp_path):
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "polonine:\n"
        "  adapter: polonine\n"
        "  source_code: pol\n"
        "  vendor: Polonine Stone Co\n"
        "  origin_default: IT\n"
        "  ports_default:\n    - Brindisi\n"
        "  mode: review\n"
        "varsha:\n"
        "  adapter: varsha\n"
        "  source_code: var\n"
        "  vendor: Varsha Stones\n"
        "  watermarked: true\n",
        encoding="utf-8")
    return yaml_path


def test_record_run_stamps_last_run_per_source(tmp_path, monkeypatch):
    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)

    # a fresh source has never run -> null label
    assert {r["source"]: r["last_run_at"] for r in store.list_rows()} == {"polonine": None, "varsha": None}

    # stamp ONE source; the other must be untouched (source-scoped label)
    store.record_run(["polonine"], "succeeded", "scrape", at="2026-07-03T15:24:13+00:00")
    rows = {r["source"]: r for r in store.list_rows()}
    assert rows["polonine"]["last_run_at"] == "2026-07-03T15:24:13+00:00"
    assert rows["polonine"]["last_run_status"] == "succeeded"
    assert rows["polonine"]["last_run_stage"] == "scrape"
    assert rows["varsha"]["last_run_at"] is None                 # not in scope -> untouched

    # an unknown source name is a harmless no-op
    store.record_run(["ghost"], "succeeded", "all")
    assert store.get_row("polonine")["last_run_status"] == "succeeded"


def test_run_log_persists_the_last_finished_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    assert store.last_run_log() is None
    store.record_run_log({"run_id": "r1", "status": "succeeded", "stage": "all",
                          "finished_at": "2026-07-03T10:00:00+00:00",
                          "counts": {"variation": 5, "product": 2, "inventory": 2}})
    store.record_run_log({"run_id": "r2", "status": "failed", "stage": "scrape",
                          "finished_at": "2026-07-03T11:00:00+00:00"})
    last = store.last_run_log()
    assert last["run_id"] == "r2"                            # most recent FINISHED
    # a still-running record (no finished_at) is never 'last'
    store.record_run_log({"run_id": "r3", "status": "running", "finished_at": None})
    assert store.last_run_log()["run_id"] == "r2"
    # upsert by run_id (a completing run overwrites its own earlier record)
    store.record_run_log({"run_id": "r2", "status": "succeeded",
                          "finished_at": "2026-07-03T11:00:00+00:00", "counts": {"variation": 9}})
    assert store.last_run_log()["counts"] == {"variation": 9}


def test_seed_pipeline_read_and_enable(tmp_path, monkeypatch):
    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))

    # seed the store from the committed yaml
    assert store.seed_from_yaml(yaml_path=yaml_path) == 2

    # the pipeline's load_sources/load_source now read the STORE (config.db exists)
    srcs = load_sources()
    assert set(srcs) == {"polonine", "varsha"}
    assert srcs["polonine"].vendor == "Polonine Stone Co"
    assert load_source("polonine").ports_default == ["Brindisi"]
    assert load_source("varsha").watermarked is True

    # all enabled by default; disabling one removes it from the run set
    assert store.enabled_names() == {"polonine", "varsha"}
    store.set_state("varsha", enabled=False)
    assert store.enabled_names() == {"polonine"}

    # re-seeding never clobbers a row the admin edited (insert-or-ignore)
    assert store.seed_from_yaml(yaml_path=yaml_path) == 0
    assert store.enabled_names() == {"polonine"}   # varsha stays disabled

    # the admin can edit a setting live
    cfg = load_source("polonine")
    cfg.vendor = "Renamed Company"
    store.upsert_source(cfg)
    assert load_source("polonine").vendor == "Renamed Company"


def test_lifecycle_defaults_to_active_and_round_trips(tmp_path, monkeypatch):
    # `lifecycle` is added by _migrate (NOT in _SCHEMA), so a freshly-seeded store already has the
    # column; legacy/never-paused rows (NULL) read as 'active'. set_lifecycle round-trips per source.
    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)
    assert {r["source"]: r["lifecycle"] for r in store.list_rows()} == {"polonine": "active", "varsha": "active"}
    store.set_state("varsha", lifecycle="paused")
    assert {r["source"]: r["lifecycle"] for r in store.list_rows()} == {"polonine": "active", "varsha": "paused"}
    # a second connect re-runs _migrate (idempotent): the value persists, no error, no duplicate column
    assert store.get_row("varsha")["lifecycle"] == "paused"


def test_enabled_names_is_none_without_a_store(tmp_path, monkeypatch):
    # no config store -> enabled_names() is None so callers run everything (pre-store behaviour)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert store.enabled_names() is None


def test_config_api_dispatch(tmp_path, monkeypatch):
    from stone_pipeline.config import server

    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)

    # list every scraper
    code, body = server.dispatch("GET", ["sources"], None)
    assert code == 200 and {s["source"] for s in body["sources"]} == {"polonine", "varsha"}

    # one scraper
    code, body = server.dispatch("GET", ["sources", "polonine"], None)
    assert code == 200 and body["vendor"] == "Polonine Stone Co"

    # the UI disables a scraper + edits a setting (PUT replaces the row)
    code, body = server.dispatch("PUT", ["sources", "varsha"],
                                 {"enabled": False, "adapter": "varsha", "source_code": "var",
                                  "vendor": "Varsha Stones"})
    assert code == 200 and body["enabled"] is False
    assert store.enabled_names() == {"polonine"}   # varsha no longer runs

    assert server.dispatch("GET", ["sources", "nope"], None)[0] == 404
    assert server.dispatch("PUT", ["sources", "x"], "not-a-dict")[0] == 400


def test_run_trigger_matches_the_admin_contract(tmp_path, monkeypatch):
    # the 'produce' button: POST /config/v1/run scrapes the ENABLED sources, async, single-run
    # guarded; GET /run = current, GET /run/<id> = by id. Contract the :4200 admin consumes.
    from stone_pipeline.config import runner, server

    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)
    store.set_state("varsha", enabled=False)                        # only polonine enabled
    runner._runs.clear(); runner._current_id = None
    try:
        rec, code = runner.start_run(launch=lambda r: None)   # launcher leaves it 'queued'
        assert code == 202 and rec["status"] == "queued" and rec["mode"] == "local"
        assert rec["sources"] == ["polonine"]                 # enabled sources only
        assert set(rec) >= {"run_id", "status", "started_at", "finished_at", "sources", "progress", "error"}
        rid = rec["run_id"]
        assert runner.start_run(launch=lambda r: None)[1] == 409                    # single-run guard
        assert server.dispatch("GET", ["run"], None)[1]["current"]["run_id"] == rid  # current
        assert server.dispatch("GET", ["run", rid], None) == (200, rec)              # by id
        assert server.dispatch("GET", ["run", "nope"], None)[0] == 404
        # a completed run frees the guard
        runner._runs.clear(); runner._current_id = None
        assert runner.start_run(launch=lambda r: r.update(status="succeeded"))[1] == 202
        assert runner.start_run(launch=lambda r: r.update(status="succeeded"))[1] == 202
    finally:
        runner._runs.clear(); runner._current_id = None
