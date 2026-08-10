"""Dimension holds at validate. A genuinely-absent dimension filled from the pack DEFAULT (a usable, positive
size) now LISTS carrying its flag -- the pack defaults are the standard size for the format, so the freight
basis is a small known estimate and an operator chose listings over exact freight. Only a genuinely-INVALID
(<=0) or a fetch-FAILED dimension still holds. The derive-side flag is unchanged (tested in test_m6_derive).
"""

from __future__ import annotations

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.stages.validate import validate_row


def _row() -> CanonicalRow:
    r = CanonicalRow(src_site="x", company_id="co_1", sales_channel_id="sc_1")
    r.inventory_quantity = 5                      # isolate the dimension behaviour from the stock gate
    return r


def test_defaulted_dimension_lists_flagged_not_held():
    r = _row()
    r.length, r.width, r.height = 2.64, 1.76, 1.76   # a usable pack default (e.g. a block whose depth defaulted)
    r.add_flag(ReviewFlag(field="width", code=FlagCode.dimension_defaulted,
                          confidence=Confidence.low, method="pack_default"))
    validate_row(r)
    assert not any(x.rule == "dimension_defaulted" for x in r.reject_reasons), "a defaulted dim must LIST, not hold"
    assert any(f.code == FlagCode.dimension_defaulted for f in r.review_flags), "the flag stays for later correction"


def test_fetch_failed_dimension_still_held():
    r = _row()                                       # derive left the dims None + flagged unavailable
    r.add_flag(ReviewFlag(field="width", code=FlagCode.dimension_unavailable,
                          confidence=Confidence.none, method="fetch_failed"))
    validate_row(r)
    assert any(x.rule == "dimension_unavailable" for x in r.reject_reasons), "a fetch-failed dim still holds for retry"


def test_zero_dimension_still_invalid():
    r = _row()
    r.length, r.width, r.height = 3.40, 0.0, 2.05    # a real size that parsed to 0 -> data error, held
    validate_row(r)
    assert any(x.rule == "dimension_invalid" for x in r.reject_reasons), "a parsed-0 dimension is a data error, held"
