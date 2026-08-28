"""One shared per-row exception boundary for the row-loop stages (normalize, match, reconcile, derive).

The binding invariant is fail-loud-and-ISOLATED: one dead row flags THAT row and never crashes the run.
The clean-pipeline stages historically iterated rows with no per-row guard, so a single unexpected exception
(an input the maintainer did not individually harden) propagated out of the stage and dropped the ENTIRE
source batch. This restores the same isolation the ledger serve path (`ledger/sync.py:_isolate`) and the
adapters already have: catch the row, dead-letter it, keep going.

A dead-lettered row is:
  - made NON-EMITTABLE (a `stage_error` RejectReason), so its partial/corrupt state can never ship;
  - SURFACED for review (a `row_stage_error` ReviewFlag carries the exception) and logged loudly;
  - SKIPPED by every later guarded stage, so a half-processed row cannot cascade or mint bogus gaps.

This is a NARROW guard, not a catch-all that masks bugs. Sparse isolation (a few bad-data rows) is the
intended case: ship the good rows, flag the dead ones. A SYSTEMIC share is a code bug, and it is escalated
twice so it can never pass as a clean success: each stage trips DEGRADED at `row_isolated_degraded`, and
run_source HARD-ABORTS (SystemExit, health FAILED) at `row_isolated_abort` -- the same fail-loud posture as
the adapt-drop / ingest / magnitude gates. The exception is also recorded verbatim on the row and logged.
"""

from __future__ import annotations

from typing import Callable

from stone_pipeline.core.schema import CanonicalRow, FlagCode, RejectReason, ReviewFlag

# The RejectReason.rule stamped on a dead-lettered row. Also the marker later guards check to skip a row that
# an earlier stage already killed -- one source of truth so the two can never diverge.
STAGE_ERROR_RULE = "stage_error"


def _already_dead(row: CanonicalRow) -> bool:
    return any(r.rule == STAGE_ERROR_RULE for r in row.reject_reasons)


def isolate_rows(rows: list[CanonicalRow], stage: str, process: Callable[[CanonicalRow], None], log) -> int:
    """Run `process(row)` for every row, isolating any that raises. Returns the count isolated THIS call
    (so the stage can trip DEGRADED on a systemic share). A row a prior guarded stage already dead-lettered
    is skipped untouched -- it is done, and re-running a later stage against its partial state would only
    risk a second exception or a spurious gap."""
    isolated = 0
    for row in rows:
        if _already_dead(row):
            continue
        try:
            process(row)
        except Exception as exc:  # deliberately broad: the whole point is to survive an UNFORESEEN row
            isolated += 1
            detail = f"{type(exc).__name__}: {exc}"[:500]
            row.add_reject(RejectReason(rule=STAGE_ERROR_RULE, detail=f"{stage}: {detail}"))
            row.add_flag(ReviewFlag(field=stage, code=FlagCode.row_stage_error, detail=detail))
            log.exception(
                "row isolated: %s stage raised on one row; dead-lettering it and continuing", stage,
                extra={"extra_fields": {"stage": stage, "surrogate_key": row.surrogate_key,
                                        "src_url": row.src_url, "error": str(exc)}},
            )
    return isolated
