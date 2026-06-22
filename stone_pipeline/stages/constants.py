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
    for row in rows:
        row.company_id = source_cfg.company_id
        row.sales_channel_id = source_cfg.sales_channel_id
        row.visibility = backend.visibility
        row.discountable = backend.discountable
    log.info("constants applied", extra={"extra_fields": {"rows": len(rows)}})
