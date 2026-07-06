"""The cold-start stall guard (cold-start review risks 1/4/5). Typing is a HARD serve gate, so a produce
that types NOTHING -- an empty type vocabulary (attributes export absent/unseeded) or 0 typed of a
non-empty produced set -- has stalled the whole catalogue and must fail LOUD, not exit 0 with a held
count that looks like the normal pass-1 hold."""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import typing_health


def _type_vocab(ledger, *values):
    now = now_iso()
    for v in values:
        ledger.upsert("attribute", {"category": "type", "value": v, "medusa_id": None,
                                    "state": "pending", "created_at": now, "updated_at": now},
                      pk=("category", "value"))


def _variation(ledger, key, type_):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": "slab", "type": type_, "name": "x",
                                "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": None,
                                "in_full": 1, "payload_hash": "", "state": "pending",
                                "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


def test_typing_health_counts_vocab_produced_typed(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _type_vocab(lg, "Marble", "Granite")
        _variation(lg, "slab_marble_x_1", "Marble")        # typed
        _variation(lg, "slab_q_y_2", "")                   # untyped
        assert typing_health(lg) == (2, 2, 1)              # (vocab, produced, typed)


def _stall(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    from stone_pipeline import produce
    return produce._cold_start_stall()


def test_empty_type_vocab_is_a_stall(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_marble_x_1", "")              # produced, but no vocab to type it
    reason = _stall(tmp_path, monkeypatch)
    assert reason and "vocabulary is EMPTY" in reason


def test_zero_typed_is_a_stall(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _type_vocab(lg, "Marble")                          # vocab present...
        _variation(lg, "slab_q_y_2", "")                   # ...but nothing typed
    reason = _stall(tmp_path, monkeypatch)
    assert reason and "0 of 1" in reason


def test_healthy_typing_is_not_a_stall(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _type_vocab(lg, "Marble")
        _variation(lg, "slab_marble_x_1", "Marble")        # typed -> serves
        _variation(lg, "slab_q_y_2", "")                   # a few untyped is the normal pass-1 hold
    assert _stall(tmp_path, monkeypatch) is None


def test_no_variations_is_not_a_stall(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _type_vocab(lg, "Marble")                          # nothing produced -> a no-op re-run, not a stall
    assert _stall(tmp_path, monkeypatch) is None
