"""C1: persist the LOCAL-disk ledger to S3 (restore on a cold task, snapshot periodically + on stop).

The ledger is the system of record for the acked Medusa id mappings. It lives on the task's LOCAL disk
(never EFS/NFS -- design section 12 / M4, and WAL requires local disk), which is ephemeral, so we
snapshot it to the env's staging bucket and restore it onto a fresh task.

Losing the last snapshot window is SAFE: a not-yet-snapshotted ack just re-serves and re-acks to the
SAME external_id-keyed Medusa id (the whole sync is idempotent by external_id), so a periodic snapshot
plus a best-effort snapshot on shutdown is enough -- no need to snapshot on every ack. No em dashes
(design principle 2).
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
import threading
from pathlib import Path

from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT, S3_BUCKET, S3_REGION
from stone_pipeline.core import logfmt

log = logfmt.get_logger("ledger.snapshot")

# Snapshot cadence (seconds). A crash loses at most this window of acks, which re-sync harmlessly.
_SNAPSHOT_INTERVAL = int(os.environ.get("BLOKPORT_LEDGER_SNAPSHOT_SECONDS", "300"))


def _s3():
    import boto3
    from botocore.config import Config
    # explicit timeouts + capped retries so a best-effort snapshot FAILS FAST instead of stalling. This
    # matters on the SIGTERM/atexit stop path: a hung S3 call must not burn the whole ECS stopTimeout and
    # get SIGKILLed with no snapshot at all. Without a Config, boto3 can also fall through to a slow IMDS
    # credential probe off-ECS.
    cfg = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2})
    return boto3.client("s3", region_name=S3_REGION, config=cfg)


def snapshot_key(env: str = ENV_NAME) -> str:
    """S3 key for the ledger snapshot. Keyed by ENV_NAME (development|production), NOT ENV_SEGMENT (dev|prod):
    this is DURABLE state, so it lives under the durable-state prefix (development/scraper/...), a sibling of --
    NOT inside -- the regenerable data plane (dev/scraper/{from_medusa,to_upload}). See settings ENV_SEGMENT/
    ENV_NAME for the boundary."""
    return f"{env}/scraper/ledger/{env}.db"


def config_key(env: str = ENV_NAME) -> str:
    """S3 key for the config-store snapshot, under the same DURABLE-state prefix as the ledger (ENV_NAME, e.g.
    development/scraper/config/config.db). config.db holds the source lifecycle (pause/delist/enabled) + the
    operator overlay, which are ephemeral on the task's /app disk -- snapshot it the same way as the ledger so a
    redeploy does not silently re-seed every source back to active or lose curation decisions."""
    return f"{env}/scraper/config/config.db"


def _consistent_copy(src: Path, dest: Path) -> None:
    """Online-consistent copy of a live SQLite db (safe while it is being written, including under WAL)
    via the backup API -- NOT a raw file copy, which could catch a torn page / partial WAL frame."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)     # page-consistent point-in-time snapshot
    finally:
        dst_conn.close()
        src_conn.close()


def save(ledger_path: str | Path, env: str = ENV_NAME, key: str | None = None) -> bool:
    """Snapshot a SQLite db to S3 (the ledger by default; `key` targets another, e.g. config.db).
    Best-effort: logs and returns False on any error, never raises into the caller (a snapshot failure
    must not take down the server or a run)."""
    key = key or snapshot_key(env)
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        _consistent_copy(ledger_path, tmp_path)
        _s3().upload_file(str(tmp_path), S3_BUCKET, key)
        log.info("db snapshot uploaded", extra={"extra_fields": {
            "key": key, "bytes": tmp_path.stat().st_size}})
        return True
    except Exception:
        log.exception("ledger snapshot failed (non-fatal)")
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def restore(ledger_path: str | Path, env: str = ENV_NAME, key: str | None = None) -> bool:
    """Restore a single file from its latest S3 snapshot into `ledger_path` (the ledger by default; `key`
    targets another -- config.db, or the combinations baseline), IF the local file is absent and a snapshot
    exists. Atomic (download to a temp then rename). Returns True if a restore happened; False (leaving the
    caller to bootstrap fresh) when the file already exists or there is no snapshot."""
    ledger_path = Path(ledger_path)
    if ledger_path.exists():
        return False                       # a local copy already wins -- never clobber it
    key = key or snapshot_key(env)
    try:
        _s3().head_object(Bucket=S3_BUCKET, Key=key)   # raises if there is no snapshot yet
    except Exception:
        log.info("no snapshot to restore; starting fresh", extra={"extra_fields": {"key": key}})
        return False
    tmp = ledger_path.with_suffix(f".restore.{os.getpid()}.tmp")   # pid-unique: both containers may boot together
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        _s3().download_file(S3_BUCKET, key, str(tmp))
        os.replace(tmp, ledger_path)       # atomic rename into place (last writer wins, same content)
        log.info("restored from snapshot", extra={"extra_fields": {"key": key}})
        return True
    except Exception:
        log.exception("snapshot restore failed (non-fatal); starting fresh")
        Path(tmp).unlink(missing_ok=True)
        return False


def save_config(config_path: str | Path, env: str = ENV_NAME) -> bool:
    """Snapshot the config store (source lifecycle) to S3. Best-effort, same as save()."""
    return save(config_path, env, key=config_key(env))


def restore_config(config_path: str | Path, env: str = ENV_NAME) -> bool:
    """Restore config.db from its S3 snapshot if the local file is absent. Run BEFORE seed_from_yaml so a
    restored config (with pause/delist state) is not masked by a fresh yaml seed."""
    return restore(config_path, env, key=config_key(env))


# -- scrape-artifact trees (outputs_dir + data/) -------------------------------
# catalog/republish consume the per-source canonical parquets (outputs_dir) and the raw scrapes (data/),
# which live on the task's EPHEMERAL disk. Only the ledger + config.db were snapshotted, so a restart
# (every deploy) wiped these and catalog/republish then found "no source runs" and aborted. Persist them
# like the ledger so a cold task restores the last scrape -- this is what makes `republish` deliver
# "release without re-scrape" durably (its whole reason to exist), not just within one task lifetime.
_ARTIFACT_TREES = ("outputs", "data")


def artifacts_key(name: str, env: str = ENV_NAME) -> str:
    """S3 key for a scrape-artifact tree tarball (`name` in _ARTIFACT_TREES)."""
    return f"{env}/scraper/artifacts/{name}.tar.gz"


def save_tree(dir_path: str | Path, key: str) -> bool:
    """Snapshot a directory tree to S3 as a gzipped tar. Best-effort (logs + returns False, never raises).
    A missing or empty dir is a no-op -- there is nothing to persist, and it must never overwrite a good
    snapshot with an empty one (e.g. a produce that failed before writing any outputs)."""
    dir_path = Path(dir_path)
    if not dir_path.exists() or not any(dir_path.iterdir()):
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(dir_path, arcname=".")
        _s3().upload_file(str(tmp_path), S3_BUCKET, key)
        log.info("artifact tree snapshot uploaded", extra={"extra_fields": {
            "key": key, "bytes": tmp_path.stat().st_size}})
        return True
    except Exception:
        log.exception("artifact tree snapshot failed (non-fatal)")
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def restore_tree(dir_path: str | Path, key: str) -> bool:
    """Restore a directory tree from its S3 tarball IF the local dir is absent or empty (never clobber a
    live scrape already on disk). Best-effort. Returns True if a restore happened."""
    dir_path = Path(dir_path)
    if dir_path.exists() and any(dir_path.iterdir()):
        return False                         # a local tree already wins
    try:
        _s3().head_object(Bucket=S3_BUCKET, Key=key)
    except Exception:
        log.info("no artifact snapshot to restore; starting fresh", extra={"extra_fields": {"key": key}})
        return False
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as t:
            tmp = Path(t.name)
        _s3().download_file(S3_BUCKET, key, str(tmp))
        dir_path.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp, "r:gz") as tar:
            tar.extractall(dir_path)         # our own snapshot (trusted content)
        log.info("artifact tree restored from snapshot", extra={"extra_fields": {"key": key}})
        return True
    except Exception:
        log.exception("artifact tree restore failed (non-fatal); starting fresh")
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def save_artifacts(env: str = ENV_NAME) -> None:
    """Persist the scrape-artifact trees after a produce, so a fresh task restores the last scrape instead
    of finding nothing to consolidate. Called once a produce has written them (config/runner)."""
    from stone_pipeline.config.settings import SETTINGS
    save_tree(SETTINGS.paths.outputs_dir, artifacts_key("outputs", env))
    save_tree(SETTINGS.paths.data_dir, artifacts_key("data", env))


def restore_artifacts(env: str = ENV_NAME) -> None:
    """Restore the scrape-artifact trees onto a cold task BEFORE any produce, mirroring the ledger restore.
    No-op when a local scrape already exists or no snapshot has been taken yet."""
    from stone_pipeline.config.settings import SETTINGS
    restore_tree(SETTINGS.paths.outputs_dir, artifacts_key("outputs", env))
    restore_tree(SETTINGS.paths.data_dir, artifacts_key("data", env))


# -- combinations delta baseline ----------------------------------------------
# tree_build emits the incremental 2_valid_combinations_update.csv by diffing the new set against the
# PREVIOUS build's to_upload/2_valid_combinations.csv. On a cold task that file is gone, so the "delta"
# balloons to the whole ~2M set (the whole point of "build on it" is to avoid that). The full file is
# already published to S3 every produce for Blokport's import (deploy.upload_artifacts), so that SAME
# object is the durable baseline -- restore it, no second snapshot.
def combinations_baseline_key() -> str:
    """S3 key of the published big-list, which doubles as the delta baseline. ENV_SEGMENT (dev/prod) is the
    Blokport-facing publish prefix that deploy.upload_artifacts writes to."""
    return f"{ENV_SEGMENT}/scraper/to_upload/2_valid_combinations.csv"


def restore_combinations_baseline() -> bool:
    """Restore the last published big-list onto a cold task so the next produce's delta ('build on it')
    stays a small increment, not the full ~2M set. No-op when a local copy already exists (a warm task's is
    fresher) or nothing has been published yet."""
    from stone_pipeline.config.settings import SETTINGS
    return restore(SETTINGS.paths.to_upload_dir / "2_valid_combinations.csv",
                   key=combinations_baseline_key())


def start_periodic(ledger_path: str | Path, env: str = ENV_NAME,
                   interval: int | None = None, key: str | None = None) -> threading.Event:
    """Start a daemon thread that snapshots a db (the ledger by default; `key` targets another, e.g.
    config.db) every `interval` seconds until the returned Event is set. The shared local volume means a
    ledger snapshot captures both containers' writes (serve leases, acks, and produce write-through)."""
    stop = threading.Event()
    every = interval or _SNAPSHOT_INTERVAL
    label = "config" if key == config_key(env) else "ledger"

    def _loop():
        while not stop.wait(every):
            save(ledger_path, env, key=key)

    threading.Thread(target=_loop, name=f"{label}-snapshot", daemon=True).start()
    log.info(f"{label} periodic snapshot started", extra={"extra_fields": {"interval_s": every}})
    return stop


def main(argv: list[str] | None = None) -> int:
    """Ops/migration CLI: `python -m stone_pipeline.ledger.snapshot save|restore`. `save` snapshots the
    ledger to S3; `restore` reseeds a fresh local-disk task from that snapshot (the acked ids)."""
    import sys

    from stone_pipeline.ledger import writethrough
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "save"
    path = writethrough.ledger_path()
    if cmd == "save":
        ok = save(path)
        print(f"snapshot save -> {'ok' if ok else 'FAILED'} ({snapshot_key()})")
        return 0 if ok else 1
    if cmd == "restore":
        ok = restore(path)
        print(f"snapshot restore -> {'restored' if ok else 'skipped (file exists or no snapshot)'}")
        return 0
    print("usage: python -m stone_pipeline.ledger.snapshot [save|restore]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
