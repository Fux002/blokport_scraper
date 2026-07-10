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
