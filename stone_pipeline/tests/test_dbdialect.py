"""Phase 4: the DB dialect knob. Default is sqlite (everything works, unchanged); a not-yet-wired dialect
fails LOUD at the connect boundary rather than as a cryptic driver error deep in the ledger/store."""

from __future__ import annotations

import pytest

from stone_pipeline.core import dbdialect


def test_default_dialect_is_sqlite(monkeypatch):
    monkeypatch.delenv("BLOKPORT_DB_DIALECT", raising=False)
    assert dbdialect.dialect() == "sqlite"
    dbdialect.require_sqlite("test")            # no raise on the default


def test_unwired_dialect_fails_loud(monkeypatch):
    monkeypatch.setenv("BLOKPORT_DB_DIALECT", "postgres")
    with pytest.raises(NotImplementedError, match="not wired yet"):
        dbdialect.require_sqlite("ledger")


def test_ledger_and_store_reject_unwired_dialect(tmp_path, monkeypatch):
    # the guard is actually wired into both connect paths.
    monkeypatch.setenv("BLOKPORT_DB_DIALECT", "postgres")
    from stone_pipeline.config import store
    from stone_pipeline.ledger.db import Ledger
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    with pytest.raises(NotImplementedError):
        store.list_rows()
    with pytest.raises(NotImplementedError):
        Ledger.open(tmp_path / "ledger.db", env="development")
