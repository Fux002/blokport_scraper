"""Process gate: the Medusa-importable output contract. Origin country code is
required for Medusa's pricing-rule lookup, so a row that still lacks one after
derive is rejected here with a precise reason rather than emitted broken. Pairs
with the derive_origin supplier-default fallback (test_m6_derive) that fills it.
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


def test_missing_origin_is_hard_rejected_with_precise_reason():
    row = _row(origin_country_code=None)
    report = gates.apply([row], gate_defs.PROCESS)
    assert not row.is_emittable  # will not emit
    assert any(r.rule == "process_missing_origin_country_code" for r in row.reject_reasons)
    assert report.rows_rejected == 1


def test_blank_origin_counts_as_missing():
    row = _row(origin_country_code="   ")
    gates.apply([row], gate_defs.PROCESS)
    assert not row.is_emittable


def test_whole_batch_missing_origin_fails():
    rows = [_row(origin_country_code="") for _ in range(5)]
    report = gates.apply(rows, gate_defs.PROCESS)
    assert report.status == gates.FAILED
    assert report.violations["process_missing_origin_country_code"] == 5
