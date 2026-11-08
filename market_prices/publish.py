"""Publish one run's folder to S3 under the shared content-hash manifest, then move the brand's latest pointer."""

from __future__ import annotations

import json
from pathlib import Path

from stone_pipeline.io.deliverables import published_manifest, publish_deliverables

from market_prices import config


def _move_latest(client, bucket: str, brand_prefix: str, brand: str, run_id: str, rows: int) -> bool:
    """Point latest at this run, unless it holds no rows or a newer run is already there: an all-blocked night or a
    late re-publish must not replace good prices. The run's folder is published either way, as the evidence."""
    key = f"{brand_prefix}/{config.LATEST_FILE}"
    if rows == 0 or published_manifest(client, bucket, key).get("run_id", "") > run_id:
        return False
    pointer = {"brand": brand, "contract_version": config.CONTRACT_VERSION,
               "manifest_key": f"{brand_prefix}/{run_id}/manifest.json", "rows": rows, "run_id": run_id}
    client.put_object(Bucket=bucket, Key=key, ContentType="application/json",
                      Body=json.dumps(pointer, indent=2, sort_keys=True).encode("utf-8"))
    return True


def publish_run(client, bucket: str, env_segment: str, brand: str, run_dir: Path) -> dict:
    """Both files, then their manifest, then latest, so a reader never sees a run that is not complete."""
    run_id = run_dir.name
    brand_prefix = config.S3_BRAND_PREFIX.format(env_segment=env_segment, brand=brand)
    published = publish_deliverables(client, bucket, run_dir, f"{brand_prefix}/{run_id}", run_id,
                                     only={config.OBSERVATIONS_FILE, config.SITE_STATUS_FILE})
    rows = published["manifest"]["files"][config.OBSERVATIONS_FILE]["rows"]
    return {**published, "latest_moved": _move_latest(client, bucket, brand_prefix, brand, run_id, rows)}
