"""SC-4 (plan SC-3-SC-4-PLAN.md section 2d): the phase 1 stores' versions. A brand-new ledger is created at v15 (one
variation row per (type, name), no branch, the per-category icon table, no combination table; product.bundle_size
stays, render-only, plan rev 2.3); a
ledger from before phase 1 is refused, never migrated in place. A brand-new config.db is stamped at the current version
(13 since P2-10 C2); one at 1 to 11 is refused (only the phase 1 store migration stamps 12, and a 12 upgrades in
place)."""
from __future__ import annotations

import sqlite3

import pytest

from stone_pipeline.config import store
from stone_pipeline.ledger.db import PHASE1_VERSION, SCHEMA_VERSION, Ledger, LedgerError


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_a_new_ledger_is_v15_one_row_per_variety_with_its_category_icons(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= PHASE1_VERSION == 15
        variation = _columns(lg.conn, "variation")
        assert not variation & {"branch", "image_url", "image_sha256", "image_model", "volume"}
        assert _columns(lg.conn, "variation_category_image") == {"key", "category_handle", "s3_key", "etag"}
        assert "bundle_size" in _columns(lg.conn, "product")      # render-only (the import template's column)
        tables = {r[0] for r in lg.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "combination" not in tables


def test_a_ledger_from_before_phase_1_is_refused_never_opened_in_place(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 14")
    with pytest.raises(LedgerError, match="phase1-migrate"):
        Ledger.open(path, env="development")


def test_a_new_config_db_is_stamped_at_the_current_version(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    with store._connect(tmp_path / "config.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 14


@pytest.mark.parametrize("version", [1, 11])
def test_a_config_db_from_before_phase_1_is_refused(tmp_path, version):
    path = tmp_path / "config.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(store.ConfigStoreError, match="phase1-migrate"):
        store._connect(path)
