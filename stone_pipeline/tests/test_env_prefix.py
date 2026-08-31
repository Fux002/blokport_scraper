"""core/env.py dual-read: SCRAPER_ wins over the legacy BLOKPORT_, but an EMPTY neutral var counts as
unset so it never shadows a real legacy value (audit A1). require() fails loud on a set-but-empty var."""

from __future__ import annotations

import pytest

from stone_pipeline.core import env


def test_scraper_prefix_wins(monkeypatch):
    monkeypatch.setenv("SCRAPER_FOO", "neutral")
    monkeypatch.setenv("BLOKPORT_FOO", "legacy")
    assert env.getenv("BLOKPORT_FOO") == "neutral"
    assert env.getenv("SCRAPER_FOO") == "neutral"
    assert env.getenv("FOO") == "neutral"


def test_legacy_fallback(monkeypatch):
    monkeypatch.delenv("SCRAPER_FOO", raising=False)
    monkeypatch.setenv("BLOKPORT_FOO", "legacy")
    assert env.getenv("BLOKPORT_FOO") == "legacy"


def test_empty_neutral_does_not_shadow_legacy(monkeypatch):
    # the migration hazard: an unset-defaulting TF var SCRAPER_X="" must NOT hide a real BLOKPORT_X.
    monkeypatch.setenv("SCRAPER_FOO", "")
    monkeypatch.setenv("BLOKPORT_FOO", "legacy")
    assert env.getenv("BLOKPORT_FOO") == "legacy"


def test_both_empty_returns_default(monkeypatch):
    monkeypatch.setenv("SCRAPER_FOO", "")
    monkeypatch.setenv("BLOKPORT_FOO", "")
    assert env.getenv("FOO", "def") == "def"
    monkeypatch.delenv("SCRAPER_FOO", raising=False)
    monkeypatch.delenv("BLOKPORT_FOO", raising=False)
    assert env.getenv("FOO", "def") == "def"


def test_require_raises_on_empty(monkeypatch):
    monkeypatch.setenv("SCRAPER_FOO", "")
    monkeypatch.delenv("BLOKPORT_FOO", raising=False)
    with pytest.raises(KeyError):
        env.require("FOO")
    monkeypatch.setenv("BLOKPORT_FOO", "legacy")
    assert env.require("FOO") == "legacy"


def test_ledger_brand_fingerprint_guard(tmp_path):
    # audit C1: a ledger stamped with one brand's fingerprint refuses to open under another's (a wrong-brand
    # snapshot restore fails loud), but a legacy ledger with no fingerprint binds first-time.
    from stone_pipeline.ledger.db import Ledger, LedgerEnvMismatch
    p = str(tmp_path / "dev.db")
    Ledger.open(p, "development", backend_id_fingerprint="brandA").close()
    Ledger.open(p, "development", backend_id_fingerprint="brandA").close()   # match -> ok
    with pytest.raises(LedgerEnvMismatch):
        Ledger.open(p, "development", backend_id_fingerprint="brandB").close()
    p2 = str(tmp_path / "legacy.db")
    Ledger.open(p2, "development").close()                                   # no fingerprint -> NULL
    Ledger.open(p2, "development", backend_id_fingerprint="brandA").close()  # first-bind, no raise
