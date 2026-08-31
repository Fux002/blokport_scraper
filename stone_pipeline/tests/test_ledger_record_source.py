"""Hermetic coverage for the write-through internals that the live equivalence test
(test_ledger_writethrough) can only exercise with the gitignored from_medusa export.

These run in CI with no scraped data and no real export:
  * seed_variations reads a SYNTHETIC variants_export.csv (the bootstrap path that forces the live test's
    skip) -- via its `path=` argument, since SETTINGS.paths is frozen.
  * record_source's own contract: no-op when the flag is off, records products when on, and NEVER raises --
    it returns False on a write failure so the caller can surface it (the ledger is the live sync source).
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import ENV_NAME
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import bootstrap, writethrough
from stone_pipeline.ledger.db import Ledger

_EXPORT_HEADER = "Id,Key,Name,Image,Aliases,Volume per kg (m³/kg)"
_VARIATION_KEY = "slab_marble_carrara_0001"


def _write_export(path):
    # the exact columns seed_variations reads; two rows, one with piped aliases
    path.write_text(
        _EXPORT_HEADER + "\n"
        f"V1,{_VARIATION_KEY},Carrara,,Statuario|Bianco,0.0012\n"
        "V2,block_granite_preto_0002,Preto,,,\n",
        encoding="utf-8",
    )
    return path


def _open_seeded(ledger_path):
    """Open the ledger the same way record_source's open_ledger does (matching fingerprint) and pre-seed
    one variation, so a later record_source sees a non-empty variation table and skips its own seeding."""
    import json

    from stone_pipeline.ledger.db import now_iso
    ledger = Ledger.open(ledger_path, env=ENV_NAME,
                         backend_id_fingerprint=writethrough.backend_fingerprint())
    now = now_iso()
    ledger.upsert("variation", {
        "key": _VARIATION_KEY, "branch": "slab", "type": "Marble", "name": "Carrara",
        "aliases": json.dumps([]), "image_url": "", "image_sha256": None, "image_model": None,
        "volume": "", "medusa_id": "V1", "payload_hash": "", "state": "synced",
        "first_seen": now, "last_synced": now, "created_at": now, "updated_at": now,
    }, pk=("key",))
    return ledger


def _rows():
    return [
        CanonicalRow(src_site="polonine", surrogate_key="AAA", variation_key=_VARIATION_KEY,
                     handle="h-aaa", title="Carrara"),
        CanonicalRow(src_site="polonine", surrogate_key="BBB", variation_key=_VARIATION_KEY,
                     handle="h-bbb", title="Carrara Two"),
    ]


def test_seed_variations_loads_synthetic_export(tmp_path):
    export = _write_export(tmp_path / "variants_export.csv")
    with Ledger.open(tmp_path / "dev.ledger", env=ENV_NAME) as ledger:
        n = bootstrap.seed_variations(ledger, path=export)
        assert n == 2
        row = ledger.execute(
            "SELECT name, aliases, medusa_id, branch FROM variation WHERE key = ?",
            (_VARIATION_KEY,)).fetchone()
    assert row["name"] == "Carrara"
    assert row["medusa_id"] == "V1"
    assert row["branch"] == "slab"
    assert "Statuario" in row["aliases"] and "Bianco" in row["aliases"]   # piped -> json list


def test_record_source_disabled_is_a_noop(tmp_path, monkeypatch):
    # flag off -> record_source must return True WITHOUT touching the ledger (open_ledger is never called).
    monkeypatch.delenv("SCRAPER_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setattr(writethrough, "open_ledger",
                        lambda *a, **k: pytest.fail("open_ledger called while disabled"))
    assert writethrough.record_source(_rows(), [], load_source("polonine"),
                                      path=tmp_path / "dev.ledger") is True


def test_record_source_enabled_records_products(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    ledger_path = tmp_path / "dev.ledger"
    _open_seeded(ledger_path).close()          # pre-seed the linked variation, matching fingerprint

    ok = writethrough.record_source(_rows(), [], load_source("polonine"), path=ledger_path)
    assert ok is True

    with Ledger.open(ledger_path, env=ENV_NAME,
                     backend_id_fingerprint=writethrough.backend_fingerprint()) as ledger:
        n = ledger.execute("SELECT COUNT(*) AS n FROM product").fetchone()["n"]
        linked = ledger.execute(
            "SELECT COUNT(*) AS n FROM product WHERE variation_key = ?", (_VARIATION_KEY,)).fetchone()["n"]
    assert n == 2, "record_source must write one product row per emitted row"
    assert linked == 2, "each product must link to its pre-seeded variation by Key"


def test_record_source_returns_false_on_failure_without_raising(tmp_path, monkeypatch):
    # A mid-write failure must be caught and surfaced as False (never a raise), so the caller can report it
    # instead of a clean success -- the ledger is the live sync source Medusa pulls.
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")

    def _boom(*a, **k):
        raise RuntimeError("simulated ledger failure")
    monkeypatch.setattr(writethrough, "open_ledger", _boom)

    result = writethrough.record_source(_rows(), [], load_source("polonine"),
                                        path=tmp_path / "dev.ledger")
    assert result is False, "a write failure must return False, not raise and not report success"
