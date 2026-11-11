"""SC-4's boot guard (plan SC-3-SC-4-PLAN.md section 2d, M2): phase 1 stores live under NEW snapshot keys (`.v2`), so
the old image can never read a migrated store. A new task must never start fresh on a stack whose OLD store exists
but whose `.v2` does not (the store migration has not run): restore treats a missing snapshot as "start fresh", and
a fresh ledger or config.db would then be uploaded as if real. Each of the three stores (the ledger in the sync
container; config.db and the learned-alias write-back in the config container) refuses that case; a brand-new stack
with neither key starts fresh as today; a migrated stack restores `.v2`."""
from __future__ import annotations

from pathlib import Path

import pytest

from stone_pipeline.ledger import snapshot

ENV = "development"


class _S3:
    """In-memory S3: `objects` by key; `head_errors` by key raise a non-absence error (a throttle)."""

    def __init__(self, objects=(), head_errors=()):
        self.objects = {k: b"x" for k in objects}
        self.head_errors = set(head_errors)

    def head_object(self, Bucket, Key):
        if Key in self.head_errors:
            exc = Exception("SlowDown")
            exc.response = {"Error": {"Code": "SlowDown"}}
            raise exc
        if Key not in self.objects:
            exc = Exception("404")
            exc.response = {"Error": {"Code": "404"}}
            raise exc

    def download_file(self, Bucket, Key, filename):
        Path(filename).write_bytes(self.objects[Key])


def _stores(tmp_path):
    """(name, restore call, its new key, its old key) for the three guarded stores."""
    wb = tmp_path / "alias_writeback.csv"
    return [
        ("ledger", lambda: snapshot.restore(tmp_path / "ledger.db", ENV, required=True),
         f"{ENV}/scraper/ledger/{ENV}.v2.db", f"{ENV}/scraper/ledger/{ENV}.db"),
        ("config", lambda: snapshot.restore_config(tmp_path / "config.db", ENV, required=True),
         f"{ENV}/scraper/config/config.v2.db", f"{ENV}/scraper/config/config.db"),
        ("writeback", lambda: snapshot.restore(wb, ENV, key=snapshot.state_writeback_key(ENV)),
         f"{ENV}/scraper/state/alias_writeback.v2.csv", f"{ENV}/scraper/state/alias_writeback.csv"),
    ]


def test_the_phase_1_stores_live_under_new_keys():
    assert snapshot.snapshot_key(ENV) == f"{ENV}/scraper/ledger/{ENV}.v2.db"
    assert snapshot.config_key(ENV) == f"{ENV}/scraper/config/config.v2.db"
    assert snapshot.state_writeback_key(ENV) == f"{ENV}/scraper/state/alias_writeback.v2.csv"


@pytest.mark.parametrize("store", ["ledger", "config", "writeback"])
def test_an_old_store_without_its_migrated_copy_refuses_to_boot(tmp_path, monkeypatch, store):
    name, call, new, old = next(s for s in _stores(tmp_path) if s[0] == store)
    monkeypatch.setattr(snapshot, "_s3", lambda: _S3(objects=[old]))
    with pytest.raises(snapshot.MigrationPending, match="phase1-migrate"):
        call()


@pytest.mark.parametrize("store", ["ledger", "config", "writeback"])
def test_a_brand_new_stack_with_neither_key_starts_fresh(tmp_path, monkeypatch, store):
    name, call, new, old = next(s for s in _stores(tmp_path) if s[0] == store)
    monkeypatch.setattr(snapshot, "_s3", lambda: _S3())
    assert call() is False


@pytest.mark.parametrize("store", ["ledger", "config", "writeback"])
def test_a_migrated_stack_restores_its_new_key(tmp_path, monkeypatch, store):
    name, call, new, old = next(s for s in _stores(tmp_path) if s[0] == store)
    monkeypatch.setattr(snapshot, "_s3", lambda: _S3(objects=[old, new]))
    assert call() is True


@pytest.mark.parametrize("store", ["ledger", "config", "writeback"])
def test_a_throttled_look_at_the_old_key_fails_loud_never_reads_as_absent(tmp_path, monkeypatch, store):
    # even the best-effort write-back: a throttle on the old key must not let a fresh .v2 be saved over nothing
    name, call, new, old = next(s for s in _stores(tmp_path) if s[0] == store)
    monkeypatch.setattr(snapshot, "_s3", lambda: _S3(head_errors=[old]))
    with pytest.raises(Exception, match="SlowDown"):
        call()
