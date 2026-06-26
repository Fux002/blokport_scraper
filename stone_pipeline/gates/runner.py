"""Apply a ModuleContract to a batch of rows, producing a GateReport."""

from __future__ import annotations

from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, RejectReason, ReviewFlag
from stone_pipeline.gates.contract import HARD, ModuleContract
from stone_pipeline.gates.report import DEGRADED, FAILED, GateReport

log = logfmt.get_logger("gates")


def apply(rows: list[CanonicalRow], contract: ModuleContract) -> GateReport:
    """Check every invariant against every row. HARD violations reject the row,
    SOFT violations with a flag_code flag it, report-only invariants only feed the
    batch status. Any single invariant violated by a large fraction of the batch
    escalates the gate's status (systemic bug, not unlucky rows).
    """
    report = GateReport(module=contract.module, rows_in=len(rows))
    rejected: set[int] = set()
    flagged: set[int] = set()
    denom = len(rows) or 1

    for inv in contract.all_invariants():
        violators = [r for r in rows if not inv.check(r)]
        if not violators:
            continue
        report.violations[inv.name] = len(violators)
        for row in violators:
            if inv.severity == HARD:
                row.add_reject(RejectReason(rule=inv.name, detail=inv.field))
                rejected.add(id(row))
            elif inv.flag_code is not None:
                row.add_flag(ReviewFlag(field=inv.field or inv.name, code=inv.flag_code,
                                        method=contract.module, src_url=row.src_url))
                flagged.add(id(row))
            # SOFT + no flag_code: report-only, no row mutation.
        fraction = len(violators) / denom
        if fraction >= contract.batch_fail_fraction:
            report.escalate(FAILED)
        elif fraction >= contract.batch_warn_fraction:
            report.escalate(DEGRADED)

    report.rows_rejected = len(rejected)
    report.rows_flagged = len(flagged)
    log.info("gate done", extra={"extra_fields": {
        "module": contract.module, "status": report.status, "rows": report.rows_in,
        "rejected": report.rows_rejected, "flagged": report.rows_flagged,
        "violations": report.violations}})
    return report
