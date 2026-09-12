"""scrapers/run.py: the `all` order honours the config store's enabled flags and never scrapes blind."""

from __future__ import annotations

import pytest

from scrapers import run as scrapers_run
from stone_pipeline.config import store


def test_enabled_order_skips_disabled_sources(monkeypatch):
    monkeypatch.setattr(store, "enabled_names", lambda: {"polonine"})
    assert scrapers_run._enabled_order() == ["polonine"]


def test_enabled_order_keeps_every_source_without_a_store(monkeypatch):
    monkeypatch.setattr(store, "enabled_names", lambda: None)
    assert scrapers_run._enabled_order() == list(scrapers_run.REGISTRY)


def test_enabled_order_fails_loud_on_a_broken_store(monkeypatch):
    # a corrupt or locked config.db must not silently live-scrape paused vendors through the metered proxy
    def _boom():
        raise RuntimeError("database disk image is malformed")
    monkeypatch.setattr(store, "enabled_names", _boom)
    with pytest.raises(RuntimeError):
        scrapers_run._enabled_order()


def test_a_failed_source_reports_its_cause_on_stderr(monkeypatch, capfd):
    # capfd (file descriptor 2), not capsys: the runner's handler holds the real stderr stream, which is
    # what the container's log driver reads
    # The produce runner streams only the subprocess's STDERR to CloudWatch. A source failure printed to
    # stdout vanished: the operator saw 'live scrape failed' and no cause. The cause, with its traceback,
    # and the run summary must all reach stderr.
    class Good:
        def run(self):
            return "/data/good/1"

    class Bad:
        def run(self):
            raise RuntimeError("CONNECT tunnel failed, response 407")

    monkeypatch.setattr(scrapers_run, "REGISTRY", {"good": Good, "bad": Bad})
    rc = scrapers_run.main(["good", "bad"])
    out, err = capfd.readouterr()
    assert rc == 1
    assert "bad failed: CONNECT tunnel failed, response 407" in err
    assert "RuntimeError" in err                       # the traceback is there
    assert "FAILED to scrape 1 source(s): ['bad']" in err
    assert "scraped 1 source(s): good -> /data/good/1" in err
    assert "failed" not in out                         # nothing about the outcome hides on stdout
