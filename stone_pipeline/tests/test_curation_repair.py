"""Incremental curation-repair primitives (the endpoints that retire the {pristine} factory reset in prod):
  - cancel_variation_tombstone  ('Not a duplicate', curation state 2) + reconcile respects the protection
  - abandon_dead_letter         (drop-one-dead-letter, curation state 3) -> terminal, auditable
The global curation rebuild (state 1) is an orchestration over _reseed_base_from_pristine + start_run and is
covered by its parts; its local no-seed guard is asserted in test_lifecycle-style paths, not here.
"""

from __future__ import annotations

import sqlite3

import pytest

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import (abandon_dead_letter, cancel_variation_tombstone, failures,
                                        ready, reconcile_variations_to_seed, record_tombstones,
                                        requeue_dead_lettered)


def _variation(ledger, key, state, medusa_id=None, name="x", in_full=1):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": key.split("_", 1)[0], "type": "Marble", "name": name,
                                "aliases": "[]", "image_url": "https://s3/tex.png", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": medusa_id,
                                "in_full": in_full, "payload_hash": "", "state": state,
                                "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


# -- schema v4 -> v5 migration (the ALTER path a deployed ledger actually takes) --

def test_v4_to_v5_migration_adds_abandoned_at_and_preserves_data(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v4 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:               # create current (v5) schema
        _variation(lg, "slab_marble_keep_1", "gap_held", medusa_id="V9")
    raw = sqlite3.connect(p)                                    # simulate a v4 ledger: strip the v5 column
    for t in ("variation", "product", "removed"):
        raw.execute(f"ALTER TABLE {t} DROP COLUMN abandoned_at")
    raw.execute("PRAGMA user_version = 4")
    raw.commit(); raw.close()

    with Ledger.open(p, env="development") as lg:               # reopen with v5 code -> _migrate runs
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        for t in ("variation", "product", "removed"):
            cols = [r[1] for r in lg.conn.execute(f"PRAGMA table_info({t})")]
            assert "abandoned_at" in cols                       # column re-added by the ALTER migration
        assert lg.get("variation", "key", "slab_marble_keep_1")["state"] == "gap_held"   # data intact
        assert abandon_dead_letter(lg, "variations", "slab_marble_keep_1")["abandoned"] is True


# -- C: abandon a dead-letter -------------------------------------------------

def test_abandon_dead_letter_is_terminal_and_auditable(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_marble_bad_1", "gap_held", medusa_id="V1")
        lg.execute("UPDATE variation SET sync_attempts = 5, sync_error = 'boom' WHERE key = ?",
                   ("slab_marble_bad_1",))

        res = abandon_dead_letter(lg, "variations", "slab_marble_bad_1")
        assert res["abandoned"] is True and res["was"] == "gap_held"
        assert lg.get("variation", "key", "slab_marble_bad_1")["abandoned_at"] is not None

        # terminal: not served, and Requeue must NOT resurrect it
        assert all(r["payload"]["key"] != "slab_marble_bad_1" for r in ready(lg, "variations"))
        assert requeue_dead_lettered(lg) == 0
        assert lg.get("variation", "key", "slab_marble_bad_1")["abandoned_at"] is not None

        # still auditable in /failures, tagged abandoned
        fs = {f["external_id"]: f for f in failures(lg)}
        assert fs["slab_marble_bad_1"]["state"] == "abandoned"
        assert fs["slab_marble_bad_1"]["error"] == "boom"

        # idempotent
        again = abandon_dead_letter(lg, "variations", "slab_marble_bad_1")
        assert again["abandoned"] is True and again.get("already") is True


def test_abandon_refuses_a_live_row_and_unknown_id(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_marble_live_1", "synced", medusa_id="V2")
        live = abandon_dead_letter(lg, "variations", "slab_marble_live_1")
        assert live["abandoned"] is False and "not a dead-letter" in live["error"]
        assert lg.get("variation", "key", "slab_marble_live_1")["state"] == "synced"   # untouched

        missing = abandon_dead_letter(lg, "variations", "nope")
        assert missing["found"] is False

        bad = abandon_dead_letter(lg, "widgets", "x")
        assert "unknown type" in bad["error"]


# -- B: 'Not a duplicate' -----------------------------------------------------

def test_cancel_variation_tombstone_restores_and_is_idempotent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_marble_twin_1", "retiring", medusa_id="V3")
        record_tombstones(lg, [("slab_marble_twin_1", None)], reason="reseed_dedup", kind="variation")

        res = cancel_variation_tombstone(lg, "slab_marble_twin_1")
        assert res["tombstone_cleared"] == 1 and res["restored"] is True and res["known"] is True
        assert lg.get("variation", "key", "slab_marble_twin_1")["state"] == "dirty"       # medusa_id -> dirty
        assert lg.execute("SELECT COUNT(*) n FROM removed WHERE external_id = ?",
                          ("slab_marble_twin_1",)).fetchone()["n"] == 0

        again = cancel_variation_tombstone(lg, "slab_marble_twin_1")     # idempotent
        assert again["tombstone_cleared"] == 0 and again["known"] is True


def test_reconcile_never_drops_a_protected_key(tmp_path):
    # two DISTINCT varieties the dedup would collapse; protecting the loser keeps BOTH (operator override).
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_marble_aqua_blue_aaaa", "synced", name="Aqua Blue")
        _variation(lg, "slab_marble_aqua_blue_bbbb", "synced", name="Aqua Blue")

        # without protection: one of the twins is dropped + tombstoned
        r1 = reconcile_variations_to_seed(lg, seed_keys=set())
        assert r1["tombstoned_dropped"] == 1
        remaining = {v["key"] for v in lg.execute("SELECT key FROM variation")}
        assert len(remaining) == 1

    with Ledger.open(tmp_path / "dev2.ledger", env="development") as lg:
        _variation(lg, "slab_marble_aqua_blue_aaaa", "synced", name="Aqua Blue")
        _variation(lg, "slab_marble_aqua_blue_bbbb", "synced", name="Aqua Blue")
        # protect BOTH -> neither is dropped
        reconcile_variations_to_seed(lg, seed_keys=set(),
                                     protected={"slab_marble_aqua_blue_aaaa", "slab_marble_aqua_blue_bbbb"})
        remaining = {v["key"] for v in lg.execute("SELECT key FROM variation")}
        assert remaining == {"slab_marble_aqua_blue_aaaa", "slab_marble_aqua_blue_bbbb"}
        assert lg.execute("SELECT COUNT(*) n FROM removed").fetchone()["n"] == 0
