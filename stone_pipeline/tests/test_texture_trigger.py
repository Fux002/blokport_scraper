"""The auto-texture trigger: after a produce queues new-variant textures the lean image can't generate, it
submits ONE GPU Batch job (RUN_MODE=generate-textures) reusing the auto-enhance queue/jobdef. Off unless
BLOKPORT_AUTO_TEXTURE; best-effort (a submit failure never raises). Mocks boto3 -- no AWS."""

from __future__ import annotations

import sys
import types

import pytest

from deploy import texture_trigger as tt


def _cfg(enabled=True, queue="q", job_definition="jd"):
    return types.SimpleNamespace(auto_texture=types.SimpleNamespace(
        enabled=enabled, queue=queue, job_definition=job_definition))


class _FakeBatch:
    def __init__(self, job_id="job-1"):
        self.calls = []
        self._job_id = job_id

    def submit_job(self, **kw):
        self.calls.append(kw)
        return {"jobId": self._job_id}


def _install_boto3(monkeypatch, batch):
    fake = types.ModuleType("boto3")
    fake.client = lambda service, region_name=None: batch
    monkeypatch.setitem(sys.modules, "boto3", fake)


def test_disabled_submits_nothing(monkeypatch):
    monkeypatch.setattr(tt, "SETTINGS", _cfg(enabled=False))
    batch = _FakeBatch()
    _install_boto3(monkeypatch, batch)
    assert tt.submit_texture_job(5) is None and batch.calls == []


def test_nothing_queued_submits_nothing(monkeypatch):
    monkeypatch.setattr(tt, "SETTINGS", _cfg())
    batch = _FakeBatch()
    _install_boto3(monkeypatch, batch)
    assert tt.submit_texture_job(0) is None and batch.calls == []


def test_missing_queue_or_jobdef_skips(monkeypatch):
    monkeypatch.setattr(tt, "SETTINGS", _cfg(queue="", job_definition=""))
    batch = _FakeBatch()
    _install_boto3(monkeypatch, batch)
    assert tt.submit_texture_job(3) is None and batch.calls == []


def test_submits_one_job_with_run_mode_override(monkeypatch):
    monkeypatch.setattr(tt, "SETTINGS", _cfg(queue="tex-q", job_definition="tex-jd"))
    batch = _FakeBatch(job_id="job-abc")
    _install_boto3(monkeypatch, batch)
    job_id = tt.submit_texture_job(7)
    assert job_id == "job-abc" and len(batch.calls) == 1
    call = batch.calls[0]
    assert call["jobQueue"] == "tex-q" and call["jobDefinition"] == "tex-jd"
    env = call["containerOverrides"]["environment"]
    assert {"name": "RUN_MODE", "value": "generate-textures"} in env   # reuse the enhance jobdef, texture mode


def test_submit_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(tt, "SETTINGS", _cfg())

    class _Boom(_FakeBatch):
        def submit_job(self, **kw):
            raise RuntimeError("batch down")

    _install_boto3(monkeypatch, _Boom())
    assert tt.submit_texture_job(2) is None   # never raises -> can't fail the produce
