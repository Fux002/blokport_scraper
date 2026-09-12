"""One produce path. The runner launches the produce subprocess in this container and watches it; there
is no second launcher that fires an unwatched Fargate task, no run-mode knob to pick one, and the batch
entrypoint's pipeline mode refuses instead of running a second, ledger-blind orchestrator."""

from __future__ import annotations

import subprocess
from pathlib import Path

from stone_pipeline.config import runner


def test_the_runner_has_one_launcher_and_no_mode_knob(monkeypatch):
    assert not hasattr(runner, "_launch_ecs") and not hasattr(runner, "_LAUNCHERS") and not hasattr(runner, "_mode")
    launched = []
    monkeypatch.setenv("SCRAPER_RUN_MODE", "ecs")                     # the old knob must do nothing
    monkeypatch.setattr(runner, "_launch_local", lambda rec: launched.append(rec["run_id"]) or rec.update(status="succeeded"))
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    rec, code = runner.start_run(["polonine"], "scrape")
    assert code == 202 and launched == [rec["run_id"]]
    assert "mode" not in rec


def test_the_batch_entrypoints_pipeline_mode_refuses():
    script = Path(__file__).resolve().parents[2] / "deploy" / "run_pipeline.sh"
    out = subprocess.run(["/bin/bash", str(script)], env={"RUN_MODE": "pipeline", "PATH": ""}, capture_output=True, text=True)
    assert out.returncode == 2
    assert "/config/v1/run" in out.stderr
