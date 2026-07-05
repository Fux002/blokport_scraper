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
import tempfile
import threading
from pathlib import Path

from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, S3_REGION
from stone_pipeline.core import logfmt

log = logfmt.get_logger("ledger.snapshot")

# Snapshot cadence (seconds). A crash loses at most this window of acks, which re-sync harmlessly.
_SNAPSHOT_INTERVAL = int(os.environ.get("BLOKPORT_LEDGER_SNAPSHOT_SECONDS", "300"))


def _s3():
    import boto3
    return boto3.client("s3", region_name=S3_REGION)


def snapshot_key(env: str = ENV_NAME) -> str:
    """S3 key for the ledger snapshot, under the env's staging tree (alongside from_medusa/ and to_upload/)."""
    return f"{env}/scraper/ledger/{env}.db"


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


def save(ledger_path: str | Path, env: str = ENV_NAME) -> bool:
    """Snapshot the ledger to S3. Best-effort: logs and returns False on any error, never raises into
    the caller (a snapshot failure must not take down the server or a run)."""
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return False
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        _consistent_copy(ledger_path, tmp_path)
        _s3().upload_file(str(tmp_path), S3_BUCKET, snapshot_key(env))
        log.info("ledger snapshot uploaded", extra={"extra_fields": {
            "key": snapshot_key(env), "bytes": tmp_path.stat().st_size}})
        return True
    except Exception:
        log.exception("ledger snapshot failed (non-fatal)")
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def restore(ledger_path: str | Path, env: str = ENV_NAME) -> bool:
    """Restore the ledger from the latest S3 snapshot into `ledger_path`, IF the local file is absent
    and a snapshot exists. Atomic (download to a temp then rename). Returns True if a restore happened;
    False (leaving the caller to bootstrap fresh) when the file already exists or there is no snapshot."""
    ledger_path = Path(ledger_path)
    if ledger_path.exists():
        return False                       # a local ledger already wins -- never clobber it
    key = snapshot_key(env)
    try:
        _s3().head_object(Bucket=S3_BUCKET, Key=key)   # raises if there is no snapshot yet
    except Exception:
        log.info("no ledger snapshot to restore; starting fresh",
                 extra={"extra_fields": {"key": key}})
        return False
    tmp = ledger_path.with_suffix(f".restore.{os.getpid()}.tmp")   # pid-unique: both containers may boot together
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        _s3().download_file(S3_BUCKET, key, str(tmp))
        os.replace(tmp, ledger_path)       # atomic rename into place (last writer wins, same content)
        log.info("ledger restored from snapshot", extra={"extra_fields": {"key": key}})
        return True
    except Exception:
        log.exception("ledger restore failed (non-fatal); starting fresh")
        Path(tmp).unlink(missing_ok=True)
        return False


def start_periodic(ledger_path: str | Path, env: str = ENV_NAME,
                   interval: int | None = None) -> threading.Event:
    """Start a daemon thread that snapshots the ledger every `interval` seconds until the returned Event
    is set. ONE server (the sync server) owns this; the shared local volume means it captures both
    containers' writes (serve leases, acks, and the config server's produce write-through)."""
    stop = threading.Event()
    every = interval or _SNAPSHOT_INTERVAL

    def _loop():
        while not stop.wait(every):
            save(ledger_path, env)

    threading.Thread(target=_loop, name="ledger-snapshot", daemon=True).start()
    log.info("ledger periodic snapshot started", extra={"extra_fields": {"interval_s": every}})
    return stop


def main(argv: list[str] | None = None) -> int:
    """Ops/migration CLI: `python -m stone_pipeline.ledger.snapshot save|restore`. `save` is the one to
    run on the CURRENT (EFS) task before the volume cutover, so the fresh local-disk task can restore
    the acked ids."""
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
