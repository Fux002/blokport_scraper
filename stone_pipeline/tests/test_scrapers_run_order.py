"""scrapers/run.py: the `all` order honours the config store's enabled flags and never scrapes blind."""

from __future__ import annotations

import threading

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


def test_sources_are_scraped_at_once(monkeypatch):
    # each scrape is network waiting on its own supplier: run side by side, a run takes its slowest source, not the
    # sum. Both fakes wait for each other, so a run of one source after the other never gets past the first.
    meet = threading.Barrier(2, timeout=5)

    class Slow:
        def run(self):
            meet.wait()
            return "/data/slow/1"

    class Fast:
        def run(self):
            meet.wait()
            return "/data/fast/1"

    monkeypatch.setattr(scrapers_run, "REGISTRY", {"slow": Slow, "fast": Fast})
    assert scrapers_run.main(["slow", "fast"]) == 0


def test_results_follow_the_order_asked_not_the_order_finished(monkeypatch):
    first_done = threading.Event()

    class Late:
        def run(self):
            assert first_done.wait(5)
            return "/data/late/1"

    class Early:
        def run(self):
            first_done.set()
            return "/data/early/1"

    monkeypatch.setattr(scrapers_run, "REGISTRY", {"late": Late, "early": Early})
    assert list(scrapers_run.run_many(["late", "early"])) == ["late", "early"]


def test_a_source_listed_twice_is_scraped_once(monkeypatch):
    # two scrapes of one source at once would write the same data/<source>/<timestamp> folder
    runs = []

    class Once:
        def run(self):
            runs.append(1)
            return "/data/once/1"

    monkeypatch.setattr(scrapers_run, "REGISTRY", {"once": Once})
    assert scrapers_run.main(["once", "once"]) == 0
    assert len(runs) == 1


def test_no_sources_is_no_run(monkeypatch):
    assert scrapers_run.run_many([]) == {}


def test_a_scrapers_stderr_lines_name_their_source(tmp_path, capfd):
    # sources scrape at once, so their lines interleave in the one log stream: each must say whose it is
    from scrapers.base import ScraperBase

    class Named(ScraperBase):
        source = "named"

    Named(data_dir=tmp_path).log.info("page 3 of 7")
    assert "INFO [named] page 3 of 7" in capfd.readouterr().err
