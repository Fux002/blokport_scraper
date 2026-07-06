"""Medusa import client interface (section 7 Stage 10, section 14 M12).

The emitter is a sink: today it writes a template-shaped CSV; later it posts to
the Medusa admin API with upsert semantics. Both implement the same ImportSink
interface, so swapping one for the other changes nothing upstream. Handles and
slugs are stable functions of the surrogate key, so the API client upserts on the
external id (the handle), never blind-inserts, which makes a re-scrape idempotent.

The HTTP client is the documented integration point; it is dry-run by default and
imported lazily so the package runs without backend credentials.

SUPERSEDED: the live Medusa integration is now the PULL-model ledger sync (ledger/sync.py
+ the /sync/v1 server) -- Medusa pulls deltas and acks ids back. This push-based ImportSink
is kept only as the documented API-sink seam (and its M12 test); the production emit writes
CSVs and the ledger serves them. Do not wire the push path without first confirming push is
still wanted over pull.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import emit

log = logfmt.get_logger("medusa")


class ImportSink(Protocol):
    def send(self, rows: list[CanonicalRow], cfg: SourceConfig) -> int:
        """Push validated rows to the destination; return the count accepted."""
        ...


class CsvImportSink:
    """The current sink: write the template-driven import CSV (Stage 10)."""

    def __init__(self, path: Path, columns: list[str] | None = None):
        self.path = path
        self.columns = columns

    def send(self, rows: list[CanonicalRow], cfg: SourceConfig) -> int:
        emit.write_import_csv(rows, cfg, self.path, self.columns)
        return len(rows)


class MedusaApiSink:
    """The later sink: upsert products into Medusa by handle. Architected here;
    the HTTP calls are the integration point and run dry by default."""

    def __init__(self, base_url: str = "", admin_token: str = "", dry_run: bool = True):
        self.base_url = base_url
        self.admin_token = admin_token
        self.dry_run = dry_run

    def _payload(self, row: CanonicalRow, cfg: SourceConfig) -> dict:
        """Map a canonical row to the Medusa product upsert payload. The handle is
        the upsert key (stable per surrogate), so a re-scrape updates in place."""
        return {
            "handle": row.handle,
            "title": row.title,
            "status": "published",
            "description": row.description,
            "category_id": row.category_pcat_id,
            "sales_channel_id": row.sales_channel_id,
            "weight": row.weight,
            "length": row.length,
            "width": row.width,
            "height": row.height,
            "images": row.image_keys,
            "metadata": {
                "stn_color_id": row.color_id,
                "stn_finish_id": row.finish_id,
                "stn_quality_id": row.quality_id,
                "stn_variation_id": row.variation_id,
                "stn_type_id": row.type_id,
                "stn_company_id": row.company_id,
                "stn_port_ids": row.port_ids,
                "stn_bundle_size": row.bundle_size,
            },
            "variants": [{
                "sku": f"{cfg.source_code}-{row.surrogate_key}".upper(),
                "title": "Default",
                "manage_inventory": True,
                "inventory_quantity": row.bundle_size or 1,
            }],
        }

    def send(self, rows: list[CanonicalRow], cfg: SourceConfig) -> int:
        if self.dry_run or not self.base_url:
            log.info("medusa sink dry-run (no upsert performed)",
                     extra={"extra_fields": {"rows": len(rows)}})
            # validate payloads are constructible even in dry-run
            for row in rows:
                self._payload(row, cfg)
            return 0
        # Integration point: POST/UPSERT each payload with the admin token,
        # upserting on handle. Left unwired until backend credentials exist.
        sent = 0
        for row in rows:
            payload = self._payload(row, cfg)  # noqa: F841 (would be posted here)
            sent += 1
        return sent
