"""What Medusa already holds, read from the ledger for validate's VE2 exemption.

A scraped product whose resolved attribute value is now archived or internal is HELD (validate's
`value_frozen` rule) UNLESS Medusa already imported THIS product carrying that value: an existing listing keeps
its value, only a NEW one is refused. "Already imported" means the ledger product row for the same SKU that
Medusa ACKED (a non-empty medusa_id). With no ledger (local / CI), there are no acked products, so every
restricted value is held. No em dashes.
"""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger
from stone_pipeline.reference.loaders.common import norm

# The product columns that carry a controlled attribute VALUE (by name). category is resolved by its pcat id,
# not a stored attribute value, so it is not part of the value-freeze exemption.
_VALUE_COLUMNS = ("color", "finish", "quality", "type")


def acked_products(ledger: Ledger) -> dict[str, set[tuple[str, str]]]:
    """{sku: {(category, norm(value)), ...}} for every product Medusa has ACKED (medusa_id set): the attribute
    values it already carries. validate exempts exactly these (sku, category, value) triples from value_frozen,
    so an existing listing on a now-archived value keeps flowing while a new product on it is held. A product
    with no medusa_id is new to Medusa and is never exempt, so it is omitted."""
    out: dict[str, set[tuple[str, str]]] = {}
    rows = ledger.execute(
        f"SELECT sku, {', '.join(_VALUE_COLUMNS)} FROM product "
        "WHERE medusa_id IS NOT NULL AND medusa_id != ''")
    for r in rows:
        carried = {(category, norm(r[category])) for category in _VALUE_COLUMNS if (r[category] or "").strip()}
        if carried:
            out[r["sku"]] = carried
    return out
