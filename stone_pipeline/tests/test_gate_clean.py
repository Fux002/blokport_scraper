"""Clean gate: after the clean module, every resolved attribute name must be the
canonical spelling for its id. This is the standing safety net for the casing-leak
bug class (a miscased 'QUARTZITE' that carries the right id but the wrong display
name -- root-caused in normalize, guarded here). Soft: it flags, never rejects.
"""

from __future__ import annotations

import pytest

from stone_pipeline import gates
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.gates import definitions as gate_defs
from stone_pipeline.reference import loaders


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


@pytest.fixture(scope="module")
def contract(ref):
    return gate_defs.clean_contract(ref)


def _row(**kw) -> CanonicalRow:
    base = dict(src_site="varsha", surrogate_key="1", type_name="Quartzite",
                color_name="Black", finish_name="Polished", quality_name="A")
    base.update(kw)
    return CanonicalRow(**base)


def test_canonical_row_passes_clean(contract):
    row = _row()
    report = gates.apply([row], contract)
    assert report.status == gates.OK
    assert not report.violations
    assert not row.review_flags


def test_miscased_type_is_flagged_not_rejected(contract):
    # one miscased row inside a healthy batch: flagged for review, still emittable, batch stays OK.
    rows = [_row() for _ in range(20)] + [_row(type_name="QUARTZITE")]
    report = gates.apply(rows, contract)
    assert report.status == gates.OK  # 1/21 is well below the warn fraction
    assert report.violations["clean_noncanonical_type_name"] == 1
    bad = rows[-1]
    assert bad.is_emittable  # casing is a display issue, the id is still valid
    assert any(f.code == FlagCode.noncanonical_attr_casing for f in bad.review_flags)


def test_unresolvable_value_is_not_treated_as_miscasing(contract):
    # a value that doesn't resolve at all is a different problem (flagged upstream); the casing
    # gate must not double-flag it.
    row = _row(type_name="Chartreuse")
    gates.apply([row], contract)
    assert not any(f.code == FlagCode.noncanonical_attr_casing for f in row.review_flags)


def test_tree_gap_is_report_only(contract):
    from stone_pipeline.core.schema import GapKind, TreeGap
    row = _row()
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="1", gap_kind=GapKind.missing_variation))
    report = gates.apply([row], contract)
    assert report.violations.get("clean_unresolved_tree_gap") == 1
    assert row.is_emittable  # report-only: the Clean gate does not reject; validate does
