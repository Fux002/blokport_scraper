"""The config server boot sequence: restore config.db, seed, reconcile, AWAIT the ledger (the sync server
is its one restorer), THEN the two-level backfill (needs the ledger), artifacts, baseline, attribute vocab.
A required restore that fails raises before anything serves. Shutdown stops the periodic snapshot BEFORE the final save (the roll-time
'cannot schedule new futures after interpreter shutdown' race) and never relies on atexit."""

from __future__ import annotations

import threading

import pytest

from stone_pipeline.config import server, store


def _record_all(monkeypatch, calls, restore_config_raises=False):
    from stone_pipeline.config import decisions_store
    from stone_pipeline.ledger import snapshot
    from deploy import fetch_inputs

    def rc(*a, **k):
        calls.append("restore_config")
        if restore_config_raises:
            raise RuntimeError("s3 down")
        return True
    monkeypatch.setattr(snapshot, "restore_config", rc)
    monkeypatch.setattr(store, "seed_from_yaml", lambda *a, **k: calls.append("seed"))
    monkeypatch.setattr(store, "reconcile_interrupted_runs", lambda *a, **k: calls.append("reconcile") or [])
    monkeypatch.setattr(snapshot, "restore", lambda *a, **k: calls.append("restore_ledger") or True)
    monkeypatch.setattr(snapshot, "await_file", lambda *a, **k: calls.append("await_ledger"))
    monkeypatch.setattr(decisions_store, "backfill_levels", lambda *a, **k: calls.append("backfill") or None)
    monkeypatch.setattr(snapshot, "restore_artifacts", lambda *a, **k: calls.append("artifacts"))
    monkeypatch.setattr(snapshot, "restore_combinations_baseline", lambda *a, **k: calls.append("baseline"))
    monkeypatch.setattr(fetch_inputs, "fetch_attributes", lambda *a, **k: calls.append("attributes") or False)


def test_boot_runs_in_the_documented_order(monkeypatch, tmp_path):
    calls: list[str] = []
    _record_all(monkeypatch, calls)
    server.boot(tmp_path / "config.db", tmp_path / "ledger.db")
    # the config container never downloads the ledger itself: two restorers racing on the shared volume could
    # replace a ledger the sync server was already serving (and acking into). It waits for the one restorer.
    assert calls == ["restore_config", "seed", "reconcile", "await_ledger", "backfill", "artifacts", "baseline", "attributes"]
    assert calls.index("backfill") > calls.index("await_ledger")          # the backfill needs the ledger


def test_a_required_restore_failure_aborts_boot_before_anything_else(monkeypatch, tmp_path):
    calls: list[str] = []
    _record_all(monkeypatch, calls, restore_config_raises=True)
    with pytest.raises(RuntimeError):
        server.boot(tmp_path / "config.db", tmp_path / "ledger.db")
    assert calls == ["restore_config"]


def test_shutdown_stops_the_periodic_snapshot_before_the_final_save(monkeypatch, tmp_path):
    from stone_pipeline.ledger import snapshot
    order: list[str] = []
    stop = threading.Event()
    monkeypatch.setattr(snapshot, "save_config", lambda *a, **k: order.append(("save", stop.is_set())))
    with pytest.raises(SystemExit):
        server.shutdown(tmp_path / "config.db", stop)
    assert order == [("save", True)]                                      # stopped first, then saved once


def test_ledger_server_shutdown_stops_the_periodic_snapshot_before_the_final_save(monkeypatch, tmp_path):
    from stone_pipeline.ledger import server as ledger_server, snapshot
    order: list = []
    stop = threading.Event()
    monkeypatch.setattr(snapshot, "save", lambda *a, **k: order.append(("save", stop.is_set())))
    with pytest.raises(SystemExit):
        ledger_server.shutdown(tmp_path / "ledger.db", stop)
    assert order == [("save", True)]
