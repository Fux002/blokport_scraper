"""Publish one run's folder to S3 under the shared content-hash manifest, then move the brand's latest pointer."""

from __future__ import annotations

import json
import re
from pathlib import Path

from stone_pipeline.io.deliverables import MANIFEST_NAME, published_manifest, publish_deliverables

from market_prices import config
from market_prices.brand import SLUG, output_key

_RUN_ID = re.compile(r"\d{8}T\d{6}Z")


def _move_latest(client, bucket: str, brand: str, run_id: str, rows: int) -> bool:
    """Point latest at this run, unless it holds no rows or a newer run is already there: an all-blocked night or a
    late re-publish must not replace good prices. The run's folder is published either way, as the evidence."""
    key = output_key(brand, config.LATEST_FILE)
    if rows == 0 or published_manifest(client, bucket, key).get("run_id", "") > run_id:
        return False
    pointer = {"brand": brand, "contract_version": config.CONTRACT_VERSION,
               "manifest_key": output_key(brand, run_id, MANIFEST_NAME), "rows": rows, "run_id": run_id}
    client.put_object(Bucket=bucket, Key=key, ContentType="application/json",
                      Body=json.dumps(pointer, indent=2, sort_keys=True).encode("utf-8"))
    return True


def publish_run(client, bucket: str, brand: str, run_dir: Path) -> dict:
    """The run's files (observations, site status, register), then their manifest (carrying the brand), then latest,
    so a reader never sees a run that is not complete. The folder must be what write_run made for this brand: its name
    becomes the run id, and a stray one would freeze latest."""
    run_id = run_dir.name
    if not SLUG.fullmatch(brand) or not _RUN_ID.fullmatch(run_id) or run_dir.parent.name != brand:
        raise ValueError(f"{run_dir} is not a run folder of brand {brand!r}")
    published = publish_deliverables(client, bucket, run_dir, output_key(brand, run_id), run_id, brand=brand,
                                     only={config.OBSERVATIONS_FILE, config.SITE_STATUS_FILE, config.REGISTER_FILE})
    rows = published["manifest"]["files"][config.OBSERVATIONS_FILE]["rows"]
    return {**published, "latest_moved": _move_latest(client, bucket, brand, run_id, rows)}
