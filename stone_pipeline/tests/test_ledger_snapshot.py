"""C1: local-disk ledger snapshot/restore to S3 (persistence for the ephemeral task volume).
Uses a fake S3 client (in-memory) so the round-trip is exercised without network."""

from __future__ import annotations

from pathlib import Path

from stone_pipeline.ledger import snapshot
from stone_pipeline.ledger.db import Ledger, now_iso


class _FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def upload_file(self, filename, bucket, key):
        self.store[(bucket, key)] = Path(filename).read_bytes()

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.store:
            raise KeyError("no such object")

    def download_file(self, Bucket, Key, filename):
        Path(filename).write_bytes(self.store[(Bucket, Key)])


def _seed(path: Path, key: str) -> None:
    now = now_iso()
    with Ledger.open(path, env="development") as lg:
        lg.upsert("variation", {"key": key, "branch": "slab", "type": "Marble", "name": "x",
                                "aliases": "[]", "image_url": "", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": "MID-1", "in_full": 1,
                                "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def test_snapshot_round_trip_preserves_acked_ids(tmp_path, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)

    src = tmp_path / "development.db"
    _seed(src, "slab_marble_test_1")

    assert snapshot.save(src, env="development") is True
    assert (S3_BUCKET_KEY := snapshot.snapshot_key("development")) in {k for _, k in fake.store}

    # a fresh, EMPTY task volume: restore brings the ledger (and its acked medusa_id) back
    dest = tmp_path / "restored" / "development.db"
    assert snapshot.restore(dest, env="development") is True
    with Ledger.open(dest, env="development") as lg:
        row = lg.get("variation", "key", "slab_marble_test_1")
        assert row is not None and row["medusa_id"] == "MID-1"   # the acked id survived the restart


def test_restore_never_clobbers_an_existing_ledger(tmp_path, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    existing = tmp_path / "development.db"
    _seed(existing, "slab_local_1")
    snapshot.save(existing, env="development")           # a snapshot exists...
    # ...but a local ledger is already present -> restore must NOT overwrite it
    assert snapshot.restore(existing, env="development") is False


def test_restore_returns_false_when_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_s3", lambda: _FakeS3())   # empty store
    assert snapshot.restore(tmp_path / "development.db", env="development") is False


def test_save_of_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_s3", lambda: _FakeS3())
    assert snapshot.save(tmp_path / "nope.db", env="development") is False
