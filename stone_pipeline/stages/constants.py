"""Stage 8: apply config constants (section 7 Stage 8).

Set the owning Medusa account, sales channel, visibility, discountable, and the
variant defaults from config. Ports are already resolved per-origin (Stage 6),
not a flat constant here.
"""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.manifest import StageMetric
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.gates.report import DEGRADED, OK

log = logfmt.get_logger("constants")


def run(rows: list[CanonicalRow], source_cfg: SourceConfig) -> StageMetric:
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
    # A blank owner on EVERY row is a systemic config miss (prod is already guarded in run.main; this
    # surfaces the same gap for dev at the layer it happens). Partial is impossible (one config value).
    missing = sum(1 for r in rows if not r.company_id or not r.sales_channel_id)
    status = DEGRADED if rows and missing == len(rows) else OK
    log.info("constants applied", extra={"extra_fields": {"rows": len(rows), "missing_owner": missing}})
    return StageMetric(stage="constants", status=status, rows_in=len(rows), rows_out=len(rows),
                       extra={"missing_owner": missing})
