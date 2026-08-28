"""The one boolean-env parser (core.env.env_bool) and the ledger write-through predicate that delegates to
it. Guards the consistency fix: the truthy vocabulary lives in a single place, the neutral SCRAPER_ prefix
wins over the legacy BLOKPORT_ fallback, and every gate reads the concept through one predicate."""

from __future__ import annotations

import pytest

from stone_pipeline.core import env


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_env_bool_truthy_tokens(monkeypatch, raw):
    monkeypatch.setenv("SCRAPER_UNIT_FLAG", raw)
    assert env.env_bool("UNIT_FLAG") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", "nonsense"])
def test_env_bool_falsy_tokens(monkeypatch, raw):
    monkeypatch.setenv("SCRAPER_UNIT_FLAG", raw)
    assert env.env_bool("UNIT_FLAG") is False


def test_env_bool_default_when_unset(monkeypatch):
    monkeypatch.delenv("SCRAPER_UNIT_FLAG", raising=False)
    monkeypatch.delenv("BLOKPORT_UNIT_FLAG", raising=False)
    assert env.env_bool("UNIT_FLAG") is False
    assert env.env_bool("UNIT_FLAG", True) is True


def test_env_bool_neutral_prefix_wins_over_legacy(monkeypatch):
    # SCRAPER_ set true, BLOKPORT_ set false -> neutral wins.
    monkeypatch.setenv("SCRAPER_UNIT_FLAG", "1")
    monkeypatch.setenv("BLOKPORT_UNIT_FLAG", "0")
    assert env.env_bool("UNIT_FLAG") is True


def test_env_bool_legacy_fallback(monkeypatch):
    # neutral unset -> the legacy BLOKPORT_ value is still honored (migration safety).
    monkeypatch.delenv("SCRAPER_UNIT_FLAG", raising=False)
    monkeypatch.setenv("BLOKPORT_UNIT_FLAG", "yes")
    assert env.env_bool("UNIT_FLAG") is True


def test_writethrough_enabled_reads_the_neutral_literal(monkeypatch):
    # The subprocess launcher writes SCRAPER_LEDGER_WRITETHROUGH; the predicate must read it.
    from stone_pipeline.ledger import writethrough
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    assert writethrough.enabled() is True
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "0")
    assert writethrough.enabled() is False


def test_writethrough_enabled_legacy_fallback(monkeypatch):
    # The existing BLOKPORT_ setters (task defs not yet re-applied, the existing test) keep working.
    from stone_pipeline.ledger import writethrough
    monkeypatch.delenv("SCRAPER_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    assert writethrough.enabled() is True
