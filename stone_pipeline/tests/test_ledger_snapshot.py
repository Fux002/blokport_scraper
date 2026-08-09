"""C1: local-disk ledger snapshot/restore to S3 (persistence for the ephemeral task volume).
Uses a fake S3 client (in-memory) so the round-trip is exercised without network."""

from __future__ import annotations

from pathlib import Path

import pytest

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


class _HeadOkDownloadFailsS3(_FakeS3):
    """head_object succeeds (the snapshot is present) but download_file raises -- the GAP-1 scenario:
    a proven-present snapshot that will not fetch (transient throttle, or a race-delete after head)."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def head_object(self, Bucket, Key):
        return                                  # present, always

    def download_file(self, Bucket, Key, filename):
        raise self._exc


def _s3_error(code: str) -> Exception:
    """A boto-shaped error carrying an Error.Code, the field s3_error_is_missing reads."""
    exc = Exception(code)
    exc.response = {"Error": {"Code": code}}    # type: ignore[attr-defined]
    return exc


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
    # ...and a required restore of a genuinely-absent snapshot still starts fresh (never a crash-loop)
    assert snapshot.restore(tmp_path / "development.db", env="development", required=True) is False


def test_required_restore_fails_loud_when_a_present_snapshot_wont_download(tmp_path, monkeypatch):
    # GAP 1: head_object proved the snapshot exists but the download fails transiently. A required (durable
    # system-of-record) restore must RAISE, not boot a fresh ledger -- booting fresh would let the guardless
    # periodic save() clobber the good snapshot. ECS restarts the task and retries.
    fake = _HeadOkDownloadFailsS3(_s3_error("SlowDown"))
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    dest = tmp_path / "development.db"
    with pytest.raises(Exception):
        snapshot.restore(dest, env="development", required=True)
    assert not dest.exists()                                  # no partial/empty ledger left behind


def test_required_restore_starts_fresh_when_snapshot_vanished_after_head(tmp_path, monkeypatch):
    # If the object genuinely disappeared between head_object and download (404/NoSuchKey), starting fresh
    # is correct even when required -- there is no snapshot left to lose, so do not crash-loop on it.
    fake = _HeadOkDownloadFailsS3(_s3_error("NoSuchKey"))
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    assert snapshot.restore(tmp_path / "development.db", env="development", required=True) is False


def test_non_required_restore_download_failure_stays_best_effort(tmp_path, monkeypatch):
    # A non-required caller (the combinations baseline, the ops CLI) must never crash boot on a download
    # failure -- it is an optimization, not the system of record.
    fake = _HeadOkDownloadFailsS3(_s3_error("SlowDown"))
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    assert snapshot.restore(tmp_path / "development.db", env="development") is False


def test_save_of_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_s3", lambda: _FakeS3())
    assert snapshot.save(tmp_path / "nope.db", env="development") is False


def test_config_snapshot_round_trip_preserves_lifecycle(tmp_path, monkeypatch):
    # E14: config.db (source lifecycle) is ephemeral on ECS; snapshot/restore it like the ledger so a
    # redeploy does not lose a pause/delist and re-seed every source back to active.
    from stone_pipeline.config import store
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    src = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(src))
    store.upsert_row({"source": "polonine",  "source_code": "pol", "vendor": "P"})
    store.set_state("polonine", lifecycle="paused", enabled=False)

    assert snapshot.save_config(src, env="development") is True
    assert snapshot.config_key("development") in {k for _, k in fake.store}

    dest = tmp_path / "restored" / "config.db"       # a fresh, empty task disk
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(dest))
    assert snapshot.restore_config(dest, env="development") is True
    row = store.get_row("polonine")
    assert row["lifecycle"] == "paused" and row["enabled"] is False   # pause survived the restart


# -- scrape-artifact TREE snapshot (outputs_dir + data/) -----------------------

def test_tree_snapshot_round_trip(tmp_path, monkeypatch):
    """A directory tree (canonical parquets / raw scrapes) survives a cold task via S3, so catalog/
    republish do not find 'no source runs' after a redeploy."""
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)

    outputs = tmp_path / "outputs"
    (outputs / "zucchi" / "run1").mkdir(parents=True)
    (outputs / "zucchi" / "run1" / "canonical.parquet").write_bytes(b"parquet-bytes")
    (outputs / "marenostone").mkdir()
    (outputs / "marenostone" / "canonical.parquet").write_bytes(b"more")

    key = snapshot.artifacts_key("outputs", "development")
    assert snapshot.save_tree(outputs, key) is True

    dest = tmp_path / "restored_outputs"
    assert snapshot.restore_tree(dest, key) is True
    assert (dest / "zucchi" / "run1" / "canonical.parquet").read_bytes() == b"parquet-bytes"
    assert (dest / "marenostone" / "canonical.parquet").read_bytes() == b"more"


def test_tree_restore_never_clobbers_a_live_scrape(tmp_path, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    key = snapshot.artifacts_key("outputs", "development")

    src = tmp_path / "outputs"
    src.mkdir()
    (src / "a.parquet").write_bytes(b"snapshot-version")
    snapshot.save_tree(src, key)

    live = tmp_path / "live"
    live.mkdir()
    (live / "fresh.parquet").write_bytes(b"on-disk")     # a scrape already present
    assert snapshot.restore_tree(live, key) is False      # must NOT overwrite it
    assert (live / "fresh.parquet").exists() and not (live / "a.parquet").exists()


def test_tree_save_of_empty_or_missing_dir_is_a_noop(tmp_path, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)
    key = snapshot.artifacts_key("data", "development")
    assert snapshot.save_tree(tmp_path / "does-not-exist", key) is False
    empty = tmp_path / "empty"; empty.mkdir()
    assert snapshot.save_tree(empty, key) is False         # never overwrite a good snapshot with nothing
    assert not fake.store


def test_combinations_baseline_key_uses_the_publish_prefix():
    # the delta baseline IS the file already published for Blokport's import (ENV_SEGMENT prefix), so the
    # two never diverge -- one durable copy, not a second snapshot.
    assert snapshot.combinations_baseline_key() == "dev/scraper/to_upload/2_valid_combinations.csv"


def test_combinations_baseline_restores_from_the_published_key(monkeypatch):
    # a cold task restores the published big-list into the LOCAL combinations file, so the next produce's
    # delta ("build on it") diffs against it instead of re-emitting the whole ~2M set. Reuses restore().
    seen: dict = {}
    monkeypatch.setattr(snapshot, "restore",
                        lambda path, key=None: seen.update(path=str(path), key=key) or True)
    assert snapshot.restore_combinations_baseline() is True
    assert seen["key"] == "dev/scraper/to_upload/2_valid_combinations.csv"        # published prefix (dev)
    assert seen["path"].endswith("to_upload/development/2_valid_combinations.csv")  # local dir (development)


def test_wipe_artifacts_drops_both_trees_local_and_s3(tmp_path, monkeypatch):
    # A pristine reset must clear the cached scrape (data/ + outputs/) LOCALLY and its S3 snapshots, so a
    # later republish/catalog cannot re-mint from it and a cold boot cannot restore it.
    deleted: list[str] = []
    fake = _FakeS3()
    fake.delete_object = lambda Bucket, Key: deleted.append(Key)   # idempotent no-op that records the key
    monkeypatch.setattr(snapshot, "_s3", lambda: fake)

    out_dir = tmp_path / "outputs"; data_dir = tmp_path / "data"
    (out_dir / "marenostone").mkdir(parents=True)
    (out_dir / "marenostone" / "canonical.parquet").write_text("x")
    (data_dir / "marenostone" / "t").mkdir(parents=True)
    (data_dir / "marenostone" / "t" / "products.csv").write_text("y")

    res = snapshot.wipe_artifacts(env="development", outputs_dir=out_dir, data_dir=data_dir)

    assert not out_dir.exists() and not data_dir.exists()          # both local trees gone
    assert set(deleted) == {snapshot.artifacts_key("outputs", "development"),
                            snapshot.artifacts_key("data", "development")}   # both S3 snapshots deleted
    assert res["local"] and len(res["s3"]) == 2

    # idempotent: re-running with the trees already gone must not raise
    snapshot.wipe_artifacts(env="development", outputs_dir=out_dir, data_dir=data_dir)


def test_wipe_artifacts_tolerates_a_missing_s3_snapshot(tmp_path, monkeypatch):
    # delete_object never errors on an absent key in real S3; a genuine error is logged, not raised.
    class _Boom(_FakeS3):
        def delete_object(self, Bucket, Key):
            raise RuntimeError("s3 down")
    monkeypatch.setattr(snapshot, "_s3", lambda: _Boom())
    # no local trees, S3 raises -> still returns (best-effort), never raises
    res = snapshot.wipe_artifacts(env="development",
                                  outputs_dir=tmp_path / "nope_out", data_dir=tmp_path / "nope_data")
    assert res["s3"] == []   # nothing recorded as deleted, but no exception escaped
