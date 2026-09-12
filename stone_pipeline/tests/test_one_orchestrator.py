"""Wave 4g: ONE orchestrator. produce owns fetch -> scrape -> build -> persist -> publish; the config runner
and the batch entrypoint both run produce and nothing else.

Before: the runner snapshotted the scrape trees and published the deliverables itself after produce exited,
and deploy/run_pipeline.sh ran fetch, scrape, pipeline, catalog and upload as five separate steps that
bypassed produce's gates, cold-start reconcile and control-plane bookkeeping. Two orchestrators, two truths.
"""

from __future__ import annotations

import subprocess

import pytest

from stone_pipeline import produce
from stone_pipeline.config import runner
from stone_pipeline.ledger import snapshot, writethrough
from deploy import upload_artifacts


@pytest.fixture
def stubbed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append("scrape") or 0)
    monkeypatch.setattr(produce, "_finalize_control_plane", lambda stage: calls.append("finalize"))
    monkeypatch.setattr(writethrough, "enabled", lambda: False)
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: calls.append("persist"))
    monkeypatch.setattr(upload_artifacts, "main", lambda run_id=None: calls.append(("publish", run_id)) or 0)
    return calls


def test_a_catalog_produce_persists_then_publishes_with_the_run_id(stubbed, monkeypatch):
    monkeypatch.setattr(produce.build, "main", lambda argv: stubbed.append("build") or 0)
    monkeypatch.setenv("SCRAPER_RUN_ID", "run-7")
    assert produce.main([]) == 0
    assert stubbed == ["fetch", "scrape", "build", "finalize", "persist", ("publish", "run-7")]


def test_a_publish_failure_fails_the_produce(stubbed, monkeypatch):
    monkeypatch.setattr(produce.build, "main", lambda argv: 0)

    def _boom(run_id=None):
        raise RuntimeError("S3 unreachable")
    monkeypatch.setattr(upload_artifacts, "main", _boom)
    assert produce.main([]) != 0


def test_a_failed_build_persists_and_publishes_nothing(stubbed, monkeypatch):
    monkeypatch.setattr(produce.build, "main", lambda argv: stubbed.append("build") or 3)
    assert produce.main([]) == 3
    assert "persist" not in stubbed and not any(isinstance(c, tuple) for c in stubbed)


def test_a_scrape_only_stage_persists_but_publishes_nothing(stubbed, monkeypatch):
    monkeypatch.setattr(produce.build, "main", lambda argv: stubbed.append("build") or 0)
    assert produce.main(["--stage", "scrape"]) == 0
    assert "persist" in stubbed and not any(isinstance(c, tuple) for c in stubbed)


def test_the_runner_hands_the_run_id_to_produce_and_publishes_nothing_itself(monkeypatch):
    seen = {}

    class _Proc:
        stderr = iter(())

        def wait(self):
            return 0

        def kill(self):
            pass

    def _popen(cmd, env=None, **kw):
        seen["env"] = env
        return _Proc()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(runner.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    runner._launch_local({"run_id": "run-9", "stage": "all", "status": "queued"})
    assert seen["env"]["SCRAPER_RUN_ID"] == "run-9"
    assert not hasattr(runner, "_publish_deliverables")
