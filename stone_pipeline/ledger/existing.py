"""What Medusa already holds, read from the ledger for validate's VE2 exemption.

A scraped product whose resolved attribute value is now archived or internal is HELD (validate's
`value_frozen` rule) UNLESS Medusa already imported THIS product carrying that value: an existing listing keeps
its value, only a NEW one is refused. "Already imported" means the ledger product row for the same SKU that
Medusa ACKED (a non-empty medusa_id). With no ledger (local / CI), there are no acked products, so every
restricted value is held. No em dashes.
"""

from __future__ import annotations

from stone_pipeline.config.categories import category_by_handle
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.reference.loaders.common import norm

# The product columns carrying a frozen-checkable VALUE: the controlled attributes (by canonical name) plus
# `variation_key`, the variety Key (VE2 PR5: a product is exempt from a frozen variety only when Medusa already
# holds it on the SAME Key). The category is stored by HANDLE (D11, populate._category_handle_for) and carried here
# as its attribute value, the label validate checks. All are folded through `norm` symmetrically for membership, so
# the Key matches regardless of its slug separators.
_VALUE_COLUMNS = ("color", "finish", "quality", "type", "category", "variation_key")


def _value(column: str, stored: str) -> str:
    """The attribute value a stored cell carries: the label for a category handle (an unknown handle raises, never
    read as some value), the cell itself otherwise."""
    if column != "category":
        return stored
    if (cat := category_by_handle(stored)) is None:
        raise ValueError(f"product category handle {stored!r} is no pack category")
    return cat.label


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
        carried = {(column, norm(_value(column, r[column]))) for column in _VALUE_COLUMNS
                   if (r[column] or "").strip()}
        if carried:
            out[r["sku"]] = carried
    return out
