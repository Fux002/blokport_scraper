"""variants_export_base.csv = the full variant list. There is ONE list, not three:

  * 1_variants_full.csv      the complete, gated, S3-imaged, Id-free variant set built each run
  * variants_export_base.csv THE SAME set, persisted as the committed seed of truth (base == full); its
                             settled bytes are also published as a deliverable (to_upload/, see write_deliverable)
  * 1_variants_update.csv     just the NEW slice of that set (a subset of full), for incremental load
  * variants_export.csv       the live Medusa state WITH ids (matching + round-trip)

So base is simply a copy of the freshly-built 1_variants_full: load full and you have loaded base;
the update is the part of it Medusa doesn't have yet. This runs at the END of catalog.run, after the
image-gate has finalised full, so the seed always equals the complete current catalog -- never a
second, diverging list. Re-runnable standalone:

    python -m stone_pipeline.reference.sync_variants_base
"""

from __future__ import annotations

import csv
import os
import shutil
import uuid
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS, THIN_COPY_MIN_FRACTION
from stone_pipeline.core import logfmt

log = logfmt.get_logger("sync_variants_base")

# FND-04: derive the env folder like every other from_medusa file (from_medusa/<env>/), never a
# hardcoded 'development' -- else a production run reads/writes this committed source-of-truth seed under
# the dev folder. from_medusa_dir follows ENV_NAME, so dev and prod each use their own base.
BASE = SETTINGS.paths.from_medusa_dir / "variants_export_base.csv"
FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
DELIVERABLE = SETTINGS.paths.variants_base_deliverable_csv
# base carries exactly the upload columns and NO Medusa Id (Medusa mints ids on import)
COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]
# Corruption guard: the base is the source of truth and a cold start seeds FROM it, so 1_variants_full can
# only grow or churn slightly (a retire drops a few). A large collapse (below settings.THIN_COPY_MIN_FRACTION) means
# the produce ran on a thin input (e.g. an empty live export), not a real catalog shrink -- refuse to overwrite the
# base with it.


def _row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8-sig") as h:
        return sum(1 for _ in csv.DictReader(h))


def publish_base_to_s3(base_path: Path) -> bool:
    """Publish the base to S3 (from_medusa/<name>) so it is the SINGLE source of truth, not a per-container
    scratch file. fetch_inputs pulls it every run, so any container -- fresh or long-lived -- reads the SAME
    base each run, eliminating the scratch-disk drift that shipped varieties in one category only. Best
    effort: a failed publish logs LOUD and leaves the local base (the run still succeeded); the next run
    republishes it. The corruption guard in sync() already refuses to write/publish a thin base. Under the S3 dry run
    (SCRAPER_S3_DRY_RUN) nothing is uploaded and it returns False."""
    from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, SETTINGS
    key = f"{ENV_SEGMENT}/scraper/from_medusa/{Path(base_path).name}"
    if SETTINGS.s3.dry_run:
        log.info("s3 dry run: base seed NOT published", extra={"extra_fields": {"key": key}})
        return False
    try:
        import boto3
        boto3.client("s3", region_name=S3_REGION).upload_file(str(base_path), S3_BUCKET, key)
        log.info("base seed published to S3 (single source of truth)", extra={"extra_fields": {"key": key}})
        return True
    except Exception:
        log.exception("base seed S3 publish FAILED: the next run may read a stale base until a later run "
                      "republishes it", extra={"extra_fields": {"key": key}})
        return False


def sync(full_path: Path = FULL, base_path: Path = BASE, write: bool = True, publish: bool = True) -> dict:
    """base := 1_variants_full (the one complete list). No-op if full hasn't been built yet; a corruption
    guard refuses a produce that would collapse the base below THIN_COPY_MIN_FRACTION of its current size. When
    `publish`, the written base is uploaded to S3 as the single source of truth (see publish_base_to_s3),
    so the container's local base can never silently drift from S3 -- the S0 determinism fix."""
    full_path, base_path = Path(full_path), Path(base_path)
    if not full_path.exists():
        log.warning("no 1_variants_full to seed base from; leaving base unchanged")
        return {"base_rows": 0, "skipped": True}
    with full_path.open(encoding="utf-8-sig") as h:
        rows = [{c: (r.get(c) or "") for c in COLS} for r in csv.DictReader(h)]
    existing = _row_count(base_path)
    if existing and len(rows) < existing * THIN_COPY_MIN_FRACTION:
        log.error("base seed NOT updated: produce looks thin; refusing to clobber the source of truth",
                  extra={"extra_fields": {"new_rows": len(rows), "base_rows": existing,
                                          "min_fraction": THIN_COPY_MIN_FRACTION}})
        return {"base_rows": existing, "new_rows": len(rows), "skipped": True, "guarded": True}
    if write:
        with base_path.open("w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        if publish:
            publish_base_to_s3(base_path)          # S0: make S3 the single source of truth for the base
    stats = {"base_rows": len(rows), "skipped": False}
    log.info("base seed updated (base == 1_variants_full)", extra={"extra_fields": stats})
    return stats


def write_deliverable(base_path: Path | None = None, deliverable_path: Path | None = None) -> None:
    """Copy the base this produce settled into to_upload/ byte for byte, so the manifest lists it (sha256, rows)
    like every deliverable and an importer verifying it against the manifest reads exactly the base the produce
    used. Called once the base is settled (after sync, whose guard may keep the fetched base). A raw file copy to a
    temp beside the target, made durable, then os.replace: the deliverable is never partial and never re-encoded.
    An absent base fails loud and first drops the previous produce's copy, so no publish can list a base that is
    not this produce's."""
    base = Path(base_path or BASE)
    deliverable = Path(deliverable_path or DELIVERABLE)
    if not base.exists():
        deliverable.unlink(missing_ok=True)
        raise FileNotFoundError(f"no settled {base.name} at {base}: the catalog cannot publish the variety base")
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    # unique per writer (pid + random), as csvio.atomic_write: overlapping processes never share a temp
    tmp = deliverable.with_name(f"{deliverable.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(base, tmp)
        with tmp.open("rb") as h:
            os.fsync(h.fileno())                    # durable before the rename
        os.replace(tmp, deliverable)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    s = sync()
    if s.get("guarded"):
        print(f"REFUSED: produce ({s['new_rows']}) would collapse the base ({s['base_rows']}); base unchanged")
    elif s["skipped"]:
        print("skipped: 1_variants_full not built yet")
    else:
        print(f"base = 1_variants_full: {s['base_rows']} variants")
