"""A run's subprocess is started in its own process group and stopped as a group, so what it started (image
generation scripts, pdftotext) is never orphaned when the run is stopped."""

from __future__ import annotations

import os
import signal
import subprocess
import time

from stone_pipeline.config.control import runner


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_stop_reaches_the_run_s_grandchildren():
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    grandchild = int(proc.stdout.readline())
    try:
        runner._signal(proc, signal.SIGTERM)
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild)
    finally:
        if _alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        proc.stdout.close()


def test_a_run_is_launched_in_its_own_session(monkeypatch):
    seen = {}

    class _Popen:
        def __init__(self, cmd, **kwargs):
            seen.update(kwargs)

    class _Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass
    monkeypatch.setattr(runner.subprocess, "Popen", _Popen)
    monkeypatch.setattr(runner.threading, "Thread", _Thread)
    monkeypatch.setattr(runner, "_stopping", False)
    try:
        runner._launch_local({"run_id": "r", "stage": "all"})
        assert seen["start_new_session"] is True
    finally:
        runner._child, runner._child_run, runner._watcher = None, None, None


def test_a_group_that_holds_only_its_exited_leader_is_nothing_to_stop(monkeypatch):
    # macOS answers PermissionError for a zombie-only group, Linux succeeds; a gone group is ProcessLookupError
    for error in (PermissionError, ProcessLookupError):
        def _killpg(pid, sig, error=error):
            raise error()
        monkeypatch.setattr(runner.os, "killpg", _killpg)
        runner._signal(type("P", (), {"pid": 12345})(), signal.SIGTERM)       # no raise
