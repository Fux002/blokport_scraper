"""The scraper config store: seed from sources.yaml, the pipeline reads it, and the
enabled flag controls which scrapers run."""

from __future__ import annotations

import json

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


def test_retired_keys_are_durable_in_config_db(tmp_path, monkeypatch):
    # the retired-variety exclusion lives in config.db (snapshotted), not a CSV under ephemeral /app, so a
    # retired variety never re-mints after a restart. Round-trip + idempotent add + un-retire remove.
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    assert store.load_retired() == set()
    store.add_retired("slab_marble_x_1")
    store.add_retired("slab_marble_x_1")               # idempotent
    store.add_retired("block_granite_y_2")
    assert store.load_retired() == {"slab_marble_x_1", "block_granite_y_2"}
    store.remove_retired("slab_marble_x_1")            # un-retire
    assert store.load_retired() == {"block_granite_y_2"}
    # decisions.load_retired is the produce-side reader and must return the same durable set
    from stone_pipeline.stages import decisions
    assert decisions.load_retired() == {"block_granite_y_2"}


def test_retired_is_empty_without_a_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert store.load_retired() == set()               # no store -> empty, never raises


def test_seed_reconciles_a_partial_config_db_keeping_state(tmp_path, monkeypatch):
    # the config.db divergence bug: a restored PARTIAL snapshot must be reconciled with sources.yaml on the
    # next boot -- the missing source re-added (active), an edited source's lifecycle state preserved.
    yaml_path = _seed_yaml(tmp_path)                         # polonine + varsha
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)
    store.set_state("polonine", lifecycle="paused", enabled=False)
    conn = store.open_store()                                # simulate a partial snapshot: varsha's row vanished
    conn.execute("DELETE FROM source WHERE source = 'varsha'"); conn.commit(); conn.close()
    assert {r["source"] for r in store.list_rows()} == {"polonine"}

    store.seed_from_yaml(yaml_path=yaml_path)                # reconcile
    rows = {r["source"]: r for r in store.list_rows()}
    assert set(rows) == {"polonine", "varsha"}              # missing source re-added
    assert rows["polonine"]["lifecycle"] == "paused" and rows["polonine"]["enabled"] is False  # state kept
    assert rows["varsha"]["lifecycle"] == "active" and rows["varsha"]["enabled"] is True        # re-added active


def test_removed_source_is_not_resurrected_by_reconcile_seed(tmp_path, monkeypatch):
    # a PERMANENTLY removed vendor must NOT come back on the next reconcile-seed (that would silently
    # re-list it); re-adding it via PUT clears the tombstone so it seeds normally again.
    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)
    assert store.delete_source("varsha") is True
    store.seed_from_yaml(yaml_path=yaml_path)
    assert {r["source"] for r in store.list_rows()} == {"polonine"}   # stays removed
    store.upsert_row({"source": "varsha", "adapter": "varsha", "source_code": "var", "vendor": "V"})
    store.seed_from_yaml(yaml_path=yaml_path)
    assert "varsha" in {r["source"] for r in store.list_rows()}       # re-add cleared the tombstone


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


def test_put_rejects_a_source_with_no_coded_adapter(tmp_path, monkeypatch):
    # ISS-3: an admin-added source whose name has no coded adapter can neither run nor be SKU-scoped, so
    # it must be refused up front instead of becoming a dead, live-looking config row.
    from stone_pipeline.config import server
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=_seed_yaml(tmp_path))
    code, body = server.dispatch("PUT", ["sources", "ghost_vendor"],
                                 {"adapter": "ghost", "source_code": "gho", "vendor": "Ghost"})
    assert code == 400 and "coded adapter" in body["error"]
    assert store.get_row("ghost_vendor") is None                     # never created


def test_put_rejects_a_duplicate_source_code(tmp_path, monkeypatch):
    # ISS-1: two sources sharing a source_code collide SKU provenance (clear/remove scope by code), so a
    # duplicate is refused. 'varsha' is a real coded adapter; giving it polonine's code 'pol' must 400.
    from stone_pipeline.config import server
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=_seed_yaml(tmp_path))              # polonine=pol, varsha=var
    code, body = server.dispatch("PUT", ["sources", "varsha"],
                                 {"adapter": "varsha", "source_code": "pol", "vendor": "V"})
    assert code == 400 and "source_code" in body["error"]
    assert store.get_row("varsha")["source_code"] == "var"           # unchanged

    # editing a real coded source with its OWN (unique) code still works
    code, body = server.dispatch("PUT", ["sources", "varsha"],
                                 {"adapter": "varsha", "source_code": "var", "vendor": "Renamed"})
    assert code == 200 and body["vendor"] == "Renamed"


# --- per-source pipeline diagnostics (Phase 1b) -------------------------------
def _fake_run(outputs_dir, source, drift=None, stage_status="OK"):
    """A minimal on-disk run: outputs/<source>_<ts>/diagnostics/{stages,health}.json."""
    d = outputs_dir / f"{source}_20260101_000000" / "diagnostics"
    d.mkdir(parents=True)
    (d / "stages.json").write_text(json.dumps({
        "run_id": f"{source}_20260101_000000", "source": source, "health": "OK",
        "magnitude": "OK", "gates": {"ingest": "OK"},
        "stages": [{"stage": "normalize", "status": stage_status, "rows_in": 3, "rows_out": 3,
                    "rejected": 0, "reviewed": 0, "gapped": 0, "extra": {}}]}), encoding="utf-8")
    (d / "health.json").write_text(json.dumps(
        {"drift": drift or [], "row_count": 3, "row_baseline": 3}), encoding="utf-8")


def test_source_diagnostic_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.record_source_diagnostic("polonine", "polonine_20260101_000000",
                                   {"source": "polonine", "health": "DEGRADED", "stages": []})
    got = store.get_source_diagnostic("polonine")
    assert got["health"] == "DEGRADED" and got["updated_at"]
    assert [d["source"] for d in store.read_source_diagnostics()] == ["polonine"]
    # a read against an ABSENT db never creates it (the isolation contract) and returns empty
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert store.get_source_diagnostic("polonine") is None
    assert store.read_source_diagnostics() == []
    assert not (tmp_path / "absent.db").exists()


def test_diagnostics_endpoint_serves_persisted(tmp_path, monkeypatch):
    from stone_pipeline.config import server
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.record_source_diagnostic("polonine", "polonine_x",
                                   {"source": "polonine", "health": "OK", "stages": []})
    assert server.dispatch("GET", ["diagnostics"], None)[1]["diagnostics"][0]["source"] == "polonine"
    code, body = server.dispatch("GET", ["diagnostics", "polonine"], None)
    assert code == 200 and body["health"] == "OK"
    assert server.dispatch("GET", ["diagnostics", "nope"], None)[0] == 404      # never produced
    assert server.dispatch("POST", ["diagnostics"], None)[0] == 405             # read-only


def test_diagnostics_disk_fallback_surfaces_drift(tmp_path, monkeypatch):
    # a source produced from the CLI (no control-plane persist) is still visible when the server shares
    # the run disk: the endpoint falls back to the latest on-disk run and folds in the health DRIFT.
    from stone_pipeline.config import diagnostics
    outputs = tmp_path / "outputs"
    _fake_run(outputs, "polonine", drift=[{"kind": "fill_drop", "field": "color"}], stage_status="DEGRADED")
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))    # nothing persisted
    got = diagnostics.read_source("polonine", outputs_dir=outputs)
    assert got is not None and got["stages"][0]["status"] == "DEGRADED"
    assert got["drift"] == [{"kind": "fill_drop", "field": "color"}]         # format change surfaced


def test_persist_from_outputs_folds_disk_into_config_db(tmp_path, monkeypatch):
    from stone_pipeline.config import diagnostics
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    outputs = tmp_path / "outputs"
    _fake_run(outputs, "polonine")                                          # polonine is a known adapter
    n = diagnostics.persist_from_outputs(outputs_dir=outputs)
    assert n >= 1
    assert store.get_source_diagnostic("polonine")["run_id"] == "polonine_20260101_000000"
