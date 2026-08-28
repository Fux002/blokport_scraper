"""keys_dedupe.run exact-dedup (F6). The product SKU is `{source}-{surrogate}`.upper(), so two natural
keys that differ ONLY in case ('ab12' vs 'AB12') collapse to ONE SKU on upload -- they MUST dedup here too
or one silently overwrites the other in Medusa (evading the guard). This pins that case-fold dedup, the
kept-first rule, and that distinct keys are both kept. The drop is logged with identity (not silent) and a
residual collision raises KeyCollisionError -- both in keys_dedupe.run.
"""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import keys_dedupe


def _row(natural: str, name: str = "Alpine White") -> CanonicalRow:
    return CanonicalRow(src_site="varsha", src_natural_key=natural, variety_match_key=name, raw_name=name)


def test_case_differing_natural_keys_dedup_to_one_kept_first():
    rows = [_row("ab12"), _row("AB12")]                 # same UPPER SKU on upload -> must collapse to one
    res = keys_dedupe.run(rows)
    assert res.dropped_exact == 1
    assert len(res.rows) == 1 and res.rows[0].surrogate_key == "ab12"   # first wins


def test_distinct_natural_keys_are_both_kept():
    res = keys_dedupe.run([_row("ab12"), _row("cd34")])
    assert res.dropped_exact == 0 and len(res.rows) == 2
