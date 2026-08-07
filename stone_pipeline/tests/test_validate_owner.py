"""Owner ids (company_id + sales_channel_id) are required at all times: a product with a blank owner is
unowned / channel-less (invisible in Medusa), so validate rejects it rather than emit an empty required
cell. This is the row-level gate every emit path shares (ledger render/writethrough, medusa_client, the
CSV import) -- not just the CLI's whole-run prod guard. In dev the owner comes from the dev defaults, so
this never fires there; it bites only a misconfigured prod.
"""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages.validate import validate_row


def test_missing_company_or_channel_is_rejected():
    r = CanonicalRow(src_site="x")                       # neither owner id set
    validate_row(r)
    assert any(x.rule == "owner_missing" for x in r.reject_reasons)

    r2 = CanonicalRow(src_site="x", company_id="co_1")   # company set, channel still missing
    validate_row(r2)
    assert any(x.rule == "owner_missing" and x.detail == "sales_channel_id" for x in r2.reject_reasons)


def test_owned_row_not_rejected_for_owner():
    r = CanonicalRow(src_site="x", company_id="co_1", sales_channel_id="sc_1")
    validate_row(r)
    assert not any(x.rule == "owner_missing" for x in r.reject_reasons)


def test_undetermined_stock_is_held_not_shipped_as_zero():
    # derive left inventory_quantity None (could neither read a count nor derive one from a stock area).
    # Emitting would ship a fabricated 0 (false out-of-stock); validate must HOLD it instead.
    r = CanonicalRow(src_site="x", company_id="co_1", sales_channel_id="sc_1")  # inventory_quantity defaults None
    validate_row(r)
    assert any(x.rule == "stock_undetermined" for x in r.reject_reasons)


def test_determined_stock_including_real_zero_is_not_held():
    # a determined value INCLUDING a real 0 (out of stock) is fine and ships as sold-out.
    for qty in (0, 5, 316):
        r = CanonicalRow(src_site="x", inventory_quantity=qty)
        validate_row(r)
        assert not any(x.rule == "stock_undetermined" for x in r.reject_reasons)


def test_missing_origin_is_hard_rejected_at_validate():
    # origin is the single-authority hard reject now (relocated from the process gate). A blank
    # origin_country_code breaks Medusa's pricing-rule lookup, so validate HOLDS it.
    for bad in (None, "", "   "):
        r = CanonicalRow(src_site="x", origin_country_code=bad)
        validate_row(r)
        assert any(x.rule == "origin_missing" for x in r.reject_reasons)
    r_ok = CanonicalRow(src_site="x", origin_country_code="IT")
    validate_row(r_ok)
    assert not any(x.rule == "origin_missing" for x in r_ok.reject_reasons)
