"""Pin every abort path of run_source: each exits 2, logs its own line, stamps (or not) the run FAILED, and writes an
exact set of files (diagnostics always; UPLOAD_STEPS.md on every path but the missing-export one). Each abort is
triggered at the seam the run itself calls, so the pin holds however run_source is laid out inside."""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from stone_pipeline import adapters as adapter_registry
from stone_pipeline import gates
from stone_pipeline import run as run_mod
from stone_pipeline.run import run_source
from stone_pipeline.stages import derive, health, magnitude_drift

SOURCE = "polonine"
RUN_DIR = "polonine_20260619_214426"
DIAGNOSTICS = {"diagnostics/manifest.json", "diagnostics/stages.json"}
WITH_HEALTH = DIAGNOSTICS | {"diagnostics/health.json"}


def _floor(monkeypatch):
    real = run_mod.load_source
    monkeypatch.setattr(run_mod, "load_source", lambda name: dataclasses.replace(real(name), min_expected_rows=10 ** 9))


def _collection_location(monkeypatch):
    def _raise(ref, cfg):
        raise ValueError("pinned: unknown collection country")
    monkeypatch.setattr(derive, "collection_location", _raise)


def _health(monkeypatch):
    real = health.run_health

    def _failed(*a, **k):
        report = real(*a, **k)
        report.status = health.FAILED
        return report
    monkeypatch.setattr(health, "run_health", _failed)


def _adapt_drop(monkeypatch):
    # armed only once health has run: health's smoke test calls the same adapt, and must still pass
    adapter, real_health, armed = adapter_registry.REGISTRY[SOURCE], health.run_health, []
    real_adapt = adapter.adapt

    def _health_then_arm(*a, **k):
        report = real_health(*a, **k)
        armed.append(True)
        return report
    monkeypatch.setattr(health, "run_health", _health_then_arm)
    monkeypatch.setattr(adapter, "adapt", lambda frame: real_adapt(frame)[: max(1, frame.height // 10)] if armed
                        else real_adapt(frame))


def _ingest_gate(monkeypatch):
    real = gates.apply

    def _failed(rows, contract):
        report = real(rows, contract)
        if report.module == "ingest":
            report.status = gates.FAILED
        return report
    monkeypatch.setattr(gates, "apply", _failed)


def _isolation(monkeypatch):
    monkeypatch.setattr(run_mod, "_isolated_fraction", lambda rows: 1.0)


def _magnitude(monkeypatch):
    real = magnitude_drift.check
    monkeypatch.setattr(magnitude_drift, "check", lambda rows, source: (magnitude_drift.FAILED, real(rows, source)[1]))


def _nothing(monkeypatch):
    pass


# name: (arm the abort, stock-only run, expected files, health_status in manifest.json, UPLOAD_STEPS health or None,
#        the exact abort log line)
STEPS = "UPLOAD_STEPS.md"
GATES_NONE, GATES_CLEAN = "Module gates: -.", "Module gates: ingest: **OK** \u00b7 clean: **OK**."
CASES = {
    "floor": (_floor, False, {STEPS} | DIAGNOSTICS, "FAILED", f"Health: **FAILED**. {GATES_NONE}",
              "scrape below floor -- failed/empty scrape; aborting (NO delist)"),
    "collection_location": (_collection_location, False, {STEPS} | DIAGNOSTICS, "FAILED",
                            f"Health: **FAILED**. {GATES_NONE}",
                            "source configuration error, holding this source (last good run stays in force): "
                            "pinned: unknown collection country"),
    "health": (_health, False, {STEPS} | WITH_HEALTH, "FAILED", f"Health: **FAILED**. {GATES_NONE}",
               "health FAILED; aborting before emit"),
    "adapt_drop": (_adapt_drop, False, {STEPS} | WITH_HEALTH, "FAILED", f"Health: **FAILED**. {GATES_NONE}",
                   "ingest: adapter dropped >50% of rows -- likely a mis-mapped required field; aborting"),
    "missing_export": (_nothing, True, WITH_HEALTH, "OK", None,
                       "inventory-only refresh aborted: the Medusa product export is missing, so no stock delta can be "
                       "computed (a '0 changed' result would be a lie)"),
    "ingest_gate": (_ingest_gate, False, {STEPS} | WITH_HEALTH, "OK", "Health: **OK**. Module gates: ingest: **FAILED**.",
                    "ingest gate FAILED; aborting before clean -- scrape is missing required fields"),
    "isolation": (_isolation, False, {STEPS} | WITH_HEALTH, "FAILED", f"Health: **FAILED**. {GATES_CLEAN}",
                  "per-row isolation breached the abort floor; aborting -- a systemic error dead-lettered too many rows "
                  "to ship this as a success"),
    "magnitude": (_magnitude, False, {STEPS, "diagnostics/magnitude_drift.json"} | WITH_HEALTH, "OK",
                  f"Health: **OK**. {GATES_CLEAN}", "magnitude drift FAILED; aborting before emit"),
}


def _run(out, data_dir, stock):
    if stock:
        return run_source(SOURCE, outputs_dir=out, state_dir=out, data_dir=data_dir,
                          known_products_path=out / "absent_products_export.csv", inventory_only=True)
    return run_source(SOURCE, outputs_dir=out, state_dir=out, data_dir=data_dir)


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_abort_exits_2_and_writes_exactly_its_files(name, tmp_path, monkeypatch, scrape_data_dir, caplog):
    arm, stock, files, status, steps_health, line = CASES[name]
    arm(monkeypatch)
    out = tmp_path / "out"
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        _run(out, scrape_data_dir, stock)
    run_dir = out / RUN_DIR
    observed = {
        "code": exc.value.code,
        "files": sorted(str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()),
        "status": json.loads((run_dir / "diagnostics" / "manifest.json").read_text(encoding="utf-8"))["health_status"],
        "steps": next((ln for ln in (run_dir / "UPLOAD_STEPS.md").read_text(encoding="utf-8").splitlines()
                       if ln.startswith("Health:")), None) if (run_dir / "UPLOAD_STEPS.md").exists() else None,
        "errors": [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR],
    }
    assert observed == {"code": 2, "files": sorted(files), "status": status, "steps": steps_health, "errors": [line]}, observed
