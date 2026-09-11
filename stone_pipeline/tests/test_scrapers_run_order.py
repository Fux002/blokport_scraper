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
