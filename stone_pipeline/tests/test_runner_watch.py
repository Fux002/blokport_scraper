"""The local produce launcher: its log is streamed line by line (never buffered until exit), a hung produce is
stopped at the timeout the same graceful way a give-way stops it, the run record keeps the tail of the log as the
failure cause, and the record always ends terminal: whatever goes wrong in the watcher, the run slot is freed."""

from __future__ import annotations

import subprocess
import threading

import pytest

from stone_pipeline.config import runner


class _FakeProc:
    """A Popen stand-in: `stderr` is an iterator of lines; wait() returns `rc` once stderr was fully read."""

    def __init__(self, lines, rc=0):
        self._lines = iter(lines)
        self.consumed = 0
        self.rc = rc
        self.signals: list[str] = []
        self.returncode = None

    @property
    def stderr(self):
        for line in self._lines:
            self.consumed += 1
            yield line

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -9 if "KILL" in self.signals else -15 if "TERM" in self.signals else self.rc
        return self.returncode

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")


class _HungProc(_FakeProc):
    """A produce that hangs after its first line until it is signalled; `honours_term` False ignores SIGTERM."""

    def __init__(self, honours_term=True):
        super().__init__([])
        self.honours_term = honours_term
        self._ended = threading.Event()

        def _hanging():
            yield "started\n"
            self._ended.wait(timeout=5)
        self._lines = _hanging()

    def wait(self, timeout=None):
        if not self._ended.wait(timeout=timeout):
            raise subprocess.TimeoutExpired("produce", timeout)
        return super().wait()

    def terminate(self):
        super().terminate()
        if self.honours_term:
            self._ended.set()

    def kill(self):
        super().kill()
        self._ended.set()


@pytest.fixture(autouse=True)
def _runner_state():
    yield
    runner._child, runner._child_run, runner._watcher = None, None, None


def _quiet(monkeypatch):
    """Stub every side effect of _watch_local except the record + the stderr stream; returns the stamped statuses."""
    stamped: list[str] = []
    for name in ("_persist_run", "_persist_diagnostics", "_evaluate_admission", "_follow_up"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_capture_counts", lambda: {})
    monkeypatch.setattr(runner, "_stamp_last_run", lambda rec, status: stamped.append(status))
    return stamped


def _watch_registered(rec, proc):
    """Run _watch_local the way _launch_local does: in its own thread, registered as the running child."""
    watcher = threading.Thread(target=runner._watch_local, args=(rec, proc), daemon=True)
    runner._child, runner._child_run, runner._watcher = proc, rec, watcher
    watcher.start()
    watcher.join(timeout=10)
    assert not watcher.is_alive()


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


def test_a_hung_produce_is_stopped_gracefully_at_the_timeout(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(runner, "RUN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 2)
    proc = _HungProc(honours_term=True)
    rec = {"run_id": "r2", "stage": "all"}
    _watch_registered(rec, proc)
    assert proc.signals == ["TERM"]                         # SIGTERM first: it cleans up and ends, no SIGKILL
    assert rec["status"] == "failed" and "timeout" in rec["error"].lower()
    assert runner._child is None                            # the slot is freed


def test_a_hung_photos_run_that_ignores_sigterm_is_killed_and_reaped_at_the_timeout(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(runner, "RUN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 0.5)
    reaped = []
    monkeypatch.setattr(runner, "_reap_photos", lambda run_id, deadline, clock=None: reaped.append(run_id))
    monkeypatch.setattr(runner, "_photo_counts", lambda rec: None)
    proc = _HungProc(honours_term=False)
    rec = {"run_id": "r3", "stage": "photos"}
    _watch_registered(rec, proc)
    assert proc.signals == ["TERM", "KILL"]                 # the grace, then the kill
    assert reaped == ["r3"]                                 # its GPU jobs, ceiling and lock are cleared
    assert rec["status"] == "failed" and "timeout" in rec["error"].lower()


def test_a_watcher_failure_after_the_run_still_records_a_terminal_status(monkeypatch):
    stamped = _quiet(monkeypatch)

    def _boom():
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(runner, "_capture_counts", _boom)
    proc = _FakeProc(["ok\n"], rc=0)
    rec = {"run_id": "r4", "stage": "all", "status": "queued"}
    runner._child, runner._child_run = proc, rec
    with pytest.raises(RuntimeError, match="ledger unreadable"):
        runner._watch_local(rec, proc)
    assert rec["status"] == "failed" and "ledger unreadable" in rec["error"]
    assert rec["finished_at"] is not None and stamped == ["failed"]
    assert runner._child is None and not runner.run_in_progress()


def test_a_watcher_failure_mid_stream_ends_the_unwatched_child(monkeypatch):
    _quiet(monkeypatch)

    class _Undecodable(_FakeProc):
        @property
        def stderr(self):
            yield "first\n"
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    proc = _Undecodable([])
    rec = {"run_id": "r5", "stage": "all"}
    with pytest.raises(UnicodeDecodeError):
        runner._watch_local(rec, proc)
    assert "KILL" in proc.signals                           # never left running with nobody reading its log
    assert rec["status"] == "failed" and "UnicodeDecodeError" in rec["error"]


def test_a_truncated_photos_summary_is_a_problem_on_the_record_not_a_crash(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r6", "stage": "photos"}
    runner._summary_path(rec).write_text('{"chosen": 3, "rev')

    class _PhotosProc:
        stderr = None                                       # a photos run streams one merged stdout
        stdout = iter(["==> done\n"])

        def wait(self, timeout=None):
            return 0
    runner._watch_local(rec, _PhotosProc())
    assert rec["status"] == "succeeded"
    assert "unreadable" in rec["counts"]["problem"]


@pytest.mark.parametrize("body", ['{"chosen": 3, "rev', "[1, 2]", "\udcff"])
def test_photo_counts_reports_an_unreadable_summary(monkeypatch, tmp_path, body):
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r7", "stage": "photos"}
    runner._summary_path(rec).write_bytes(body.encode("utf-8", "surrogateescape"))
    assert "problem" in runner._photo_counts(rec)


def test_the_kept_tail_and_the_run_history_are_settings(monkeypatch):
    from stone_pipeline.config import settings
    assert runner.RUN_LOG_TAIL_LINES == settings.RUN_LOG_TAIL_LINES and runner.RUN_HISTORY_MAX == settings.RUN_HISTORY_MAX
    _quiet(monkeypatch)
    monkeypatch.setattr(runner, "RUN_LOG_TAIL_LINES", 3)
    rec = {"run_id": "r8", "stage": "all"}
    runner._watch_local(rec, _FakeProc([f"line {i}\n" for i in range(10)], rc=1))
    assert rec["error"].splitlines()[1:] == ["line 7", "line 8", "line 9"]
