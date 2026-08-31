"""The per-row exception boundary (stages/_rowguard.py): one dead row flags THAT row and the run
continues -- the fail-loud-and-ISOLATED invariant the clean-pipeline stages must uphold. Before this,
an unexpected exception on one row propagated out of the stage and dropped the whole source batch.
"""

from __future__ import annotations

import logging

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.stages._rowguard import STAGE_ERROR_RULE, isolate_rows

log = logging.getLogger("test_rowguard")


def _rows(n: int) -> list[CanonicalRow]:
    return [CanonicalRow(src_site="varsha", surrogate_key=f"s{i}") for i in range(n)]


def test_one_raising_row_is_isolated_the_rest_survive():
    rows = _rows(3)
    seen: list[str] = []

    def process(r: CanonicalRow) -> None:
        if r.surrogate_key == "s1":
            raise ValueError("boom on the middle row")
        seen.append(r.surrogate_key)
        r.title = "processed"

    isolated = isolate_rows(rows, "derive", process, log)

    assert isolated == 1
    assert seen == ["s0", "s2"]                       # the other two ran to completion
    dead = rows[1]
    assert not dead.is_emittable                      # dead-lettered -> never ships
    assert any(rr.rule == STAGE_ERROR_RULE and "derive" in rr.detail for rr in dead.reject_reasons)
    assert any(f.code == FlagCode.row_stage_error and "boom" in (f.detail or "") for f in dead.review_flags)
    assert rows[0].is_emittable and rows[2].is_emittable   # survivors are untouched


def test_already_dead_row_is_skipped_by_a_later_stage():
    # a row an earlier guarded stage dead-lettered must NOT be re-processed downstream: its partial state
    # would only risk a second exception or a spurious gap. The guard skips it untouched.
    rows = _rows(2)
    isolate_rows(rows, "normalize", lambda r: (_ for _ in ()).throw(RuntimeError("die")) if r.surrogate_key == "s0" else None, log)
    assert not rows[0].is_emittable

    touched: list[str] = []
    isolated = isolate_rows(rows, "derive", lambda r: touched.append(r.surrogate_key), log)

    assert touched == ["s1"]                           # s0 skipped, not re-run
    assert isolated == 0                               # skipping is not an isolation event
    # the dead row still carries exactly ONE stage_error (from normalize), not a second from derive
    assert sum(1 for rr in rows[0].reject_reasons if rr.rule == STAGE_ERROR_RULE) == 1


def test_clean_batch_is_a_no_op():
    rows = _rows(4)
    isolated = isolate_rows(rows, "reconcile", lambda r: None, log)
    assert isolated == 0
    assert all(r.is_emittable for r in rows)
    assert all(not r.review_flags for r in rows)


def test_isolated_fraction_drives_the_systemic_abort_gate():
    # run_source hard-aborts when the dead-lettered share reaches row_isolated_abort (a systemic code bug),
    # instead of shipping a gutted batch as a clean success. This pins the SIGNAL that gate reads: the
    # fraction of rows carrying a stage_error reject. Sparse isolation stays below the floor; an all-fail
    # batch is 1.0.
    from stone_pipeline import run as run_mod
    from stone_pipeline.config.settings import SETTINGS
    abort_floor = SETTINGS.thresholds.row_isolated_abort

    assert run_mod._isolated_fraction([]) == 0.0                      # empty batch: no abort

    sparse = _rows(100)
    isolate_rows(sparse, "derive", lambda r: (_ for _ in ()).throw(ValueError("x")) if r.surrogate_key == "s0" else None, log)
    assert run_mod._isolated_fraction(sparse) == 0.01
    assert run_mod._isolated_fraction(sparse) < abort_floor           # 1 bad row ships the other 99

    systemic = _rows(10)
    isolate_rows(systemic, "derive", lambda r: (_ for _ in ()).throw(KeyError("typo")), log)
    assert run_mod._isolated_fraction(systemic) == 1.0                # every row hit the bug
    assert run_mod._isolated_fraction(systemic) >= abort_floor        # -> hard abort
