"""Publish one supplier_prices run: prices.csv, then the manifest naming its sha256, rows and version, written once."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io.deliverables import (MANIFEST_NAME, RunAlreadyPublished, publish_deliverables,
                                            refuse_published_run)
from supplier_prices import config
from supplier_prices.writer import RunWritten

__all__ = ["RunAlreadyPublished", "publish_run"]


def run_prefix(brand: str, run_id: str) -> str:
    return f"{config.KEY_PREFIX.format(env_segment=ENV_SEGMENT, brand=brand)}/{run_id}"


def publish_run(client, bucket: str, brand: str, written: RunWritten) -> dict:
    """The run's prices.csv, then its manifest (the contract, its version and the vendor), so a reader never sees a run
    that is not complete. A run already published is refused before anything is written."""
    if written.run_dir.name != written.run_id or written.run_dir.parent.name != brand:
        raise ValueError(f"{written.run_dir} is not run {written.run_id} of brand {brand!r}")
    prefix = run_prefix(brand, written.run_id)
    refuse_published_run(client, bucket, f"{prefix}/{MANIFEST_NAME}", written.run_id)
    return publish_deliverables(client, bucket, written.run_dir, prefix, written.run_id, only={config.PRICES_FILE},
                                brand=brand, header={"contract": config.CONTRACT,
                                                     "contract_version": config.CONTRACT_VERSION,
                                                     "vendor": written.vendor})
