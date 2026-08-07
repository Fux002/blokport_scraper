"""TEMPORARY one-off backfill -- REMOVE after use (see the PR that adds this).

Re-serve the "stranded image" class: variants whose texture is in the ledger (image_url + image_sha256
set) and marked `synced`, but whose Key shows an EMPTY Image in the current Medusa export. These acked the
image-inclusive payload_hash (so they read synced) yet Medusa dropped the image on ingest, so they never
self-heal. Setting them `dirty` makes the next pull re-serve them through the (now-fixed) ingest.

Idempotent: once Medusa has the image and re-acks, they return to synced and are no longer selected. Runs
in-cluster against the live local ledger (the only place a ledger write is safe -- the S3 copy is a
snapshot that every external write races). WAL lets this run alongside the sync server.
"""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.ledger.db import Ledger, now_iso

log = logfmt.get_logger("ledger.reserve")


def _keys_missing_image_in_export(export_path: Path) -> set[str]:
    """Keys whose row in the Medusa export has an empty Image cell (Medusa has no image for them)."""
    if not export_path.exists():
        return set()
    with export_path.open(encoding="utf-8-sig", newline="") as handle:
        return {key for r in csv.DictReader(handle)
                if (key := (r.get("Key") or "").strip()) and not (r.get("Image") or "").strip()}


def reserve_stranded_images(ledger: Ledger, export_path: Path | None = None) -> dict:
    export_path = Path(export_path or SETTINGS.paths.variants_export_csv)
    missing = _keys_missing_image_in_export(export_path)
    imaged_synced = [r["key"] for r in ledger.execute(
        "SELECT key FROM variation WHERE image_url IS NOT NULL AND image_url != '' "
        "AND image_sha256 IS NOT NULL AND state = 'synced'")]
    stranded = [k for k in imaged_synced if k in missing]
    if stranded:
        now = now_iso()
        ledger.conn.executemany(
            "UPDATE variation SET state = 'dirty', updated_at = ? WHERE key = ?",
            [(now, k) for k in stranded])
        ledger.conn.commit()
    result = {"reserved": len(stranded), "imaged_synced": len(imaged_synced),
              "export_present": export_path.exists(), "sample": stranded[:5]}
    log.info("reserve stranded images", extra={"extra_fields": result})
    return result
