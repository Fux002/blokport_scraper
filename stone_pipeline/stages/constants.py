"""Stage 8: apply config constants (section 7 Stage 8).

Set the owning Medusa account, sales channel, visibility, discountable, and the
variant defaults from config. Ports are already resolved per-origin (Stage 6),
not a flat constant here.
"""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow

log = logfmt.get_logger("constants")


def run(rows: list[CanonicalRow], source_cfg: SourceConfig) -> None:
    backend = SETTINGS.backend
    # owner ids are env config (settings, from env vars), never hardcoded here. sales_channel is
    # one id per env; company is the general Blokport default unless this scrape overrides it.
    company = source_cfg.company_id or backend.company_id
    sales_channel = backend.sales_channel_id
    for row in rows:
        row.company_id = company
        row.sales_channel_id = sales_channel
        row.visibility = backend.visibility
        row.discountable = backend.discountable
    log.info("constants applied", extra={"extra_fields": {"rows": len(rows)}})
