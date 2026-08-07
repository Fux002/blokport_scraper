"""Process gate: after derive, a missing origin country code is surfaced as a SOFT EARLY DIAGNOSTIC --
a systemic origin-resolution failure escalates this seam's report for fast repair. The HARD reject that
actually HOLDS an origin-less row is owned by validate (Stage 9), the single Medusa-importable authority
(see test_validate_owner.test_missing_origin_is_hard_rejected_at_validate). Origin is thus confirmed in
one place; the process seam only diagnoses it early.
"""

from __future__ import annotations

from stone_pipeline import gates
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.gates import definitions as gate_defs


def _row(**kw) -> CanonicalRow:
    base = dict(src_site="polonine", surrogate_key="620", origin_country_code="IT")
    base.update(kw)
    return CanonicalRow(**base)


def test_row_with_origin_passes():
    row = _row()
    report = gates.apply([row], gate_defs.PROCESS)
    assert report.status == gates.OK
    assert row.is_emittable


def test_missing_origin_is_flagged_early_but_not_rejected_here():
    # SOFT diagnostic: the seam reports the missing origin, but the row stays emittable at THIS gate --
    # the hard hold is validate's job (single authority), so the reject is not duplicated here.
    row = _row(origin_country_code=None)
    report = gates.apply([row], gate_defs.PROCESS)
    assert report.violations["process_origin_missing"] == 1
    assert row.is_emittable


def test_blank_origin_counts_as_missing():
    row = _row(origin_country_code="   ")
    report = gates.apply([row], gate_defs.PROCESS)
    assert report.violations["process_origin_missing"] == 1


def test_whole_batch_missing_origin_escalates_the_report():
    # a systemic failure (a whole batch with no origin) surfaces here as FAILED status, for fast repair.
    rows = [_row(origin_country_code="") for _ in range(5)]
    report = gates.apply(rows, gate_defs.PROCESS)
    assert report.status == gates.FAILED
    assert report.violations["process_origin_missing"] == 5
