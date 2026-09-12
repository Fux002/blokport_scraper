"""The local produce launcher: its log is streamed line by line (never buffered until exit), a hung produce is
killed at the timeout, and the run record keeps the tail of the log as the failure cause."""

from __future__ import annotations

import threading

from stone_pipeline.config import runner


class _FakeProc:
    """A Popen stand-in: `stderr` is an iterator of lines; wait() returns `rc` once stderr was fully read."""

    def __init__(self, lines, rc=0):
        self._lines = iter(lines)
        self.consumed = 0
        self.rc = rc
        self.killed = False
        self.returncode = None

    @property
    def stderr(self):
        for line in self._lines:
            self.consumed += 1
            yield line

    def wait(self):
        self.returncode = -9 if self.killed else self.rc
        return self.returncode

    def kill(self):
        self.killed = True


def _quiet(monkeypatch):
    """Stub every side effect of _watch_local except the record + the stderr stream."""
    for name in ("_capture_counts", "_stamp_last_run", "_persist_run", "_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda *a, **k: {} if name == "_capture_counts" else None)
    monkeypatch.setattr(runner, "_publish_deliverables", lambda *a, **k: None)
    from stone_pipeline.ledger import snapshot
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)


def test_produce_log_is_streamed_and_the_tail_is_kept_on_failure(monkeypatch, capsys):
    _quiet(monkeypatch)
    lines = [f"line {i}\n" for i in range(50)]
    proc = _FakeProc(lines, rc=1)
    rec = {"run_id": "r1", "stage": "all"}
    runner._watch_local(rec, proc)
    assert proc.consumed == 50                              # the stream was read, not communicate()d
    assert rec["status"] == "failed"
    assert "line 49" in rec["error"] and "line 0" not in rec["error"]     # tail only (40 lines)
    err = capsys.readouterr().err
    assert "line 0" in err and "line 49" in err               # every line reached this process's stderr


def test_a_hung_produce_is_killed_at_the_timeout(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(runner, "_RUN_TIMEOUT", 0.2)
    release = threading.Event()

    def _hanging():
        yield "started\n"
        release.wait(timeout=5)                             # blocks until kill() releases it
        yield "after kill\n"
    proc = _FakeProc([], rc=0)
    proc._lines = _hanging()
    proc.kill = lambda: (setattr(proc, "killed", True), release.set())
    rec = {"run_id": "r2", "stage": "all"}
    runner._watch_local(rec, proc)
    assert proc.killed and rec["status"] == "failed" and "timeout" in rec["error"].lower()
