"""Test helpers for the category registry (table 577): a category's pcat is the registry's, keyed by its exact handle,
so a test that prices or renders under its own pcat ids sets them on the registry (conftest's tree_category_pcats
and slab_category_pcat use set_category_pcats), and a test that writes an attributes.csv gives each category row its
handle. No em dashes.
"""

from __future__ import annotations

from dataclasses import replace

from stone_pipeline.config import categories

# the pcat ids the tree-build tests write into their attributes.csv
TREE_PCATS = {"slab": "cat_slab", "block": "cat_block", "tile": "cat_tile"}
# the pcat ids test_ledger_products' canonical rows carry (its _fixture_rows), shared by the modules that import them
LEDGER_PRODUCT_PCATS = {"slab": "PC1", "block": "PC2"}
# the stone pack's category label -> its handle (D11), for writing a category row the way T1 does
HANDLE_OF_LABEL = {c.label: c.handle for c in categories.CATEGORIES}


def set_category_pcats(monkeypatch, pcats: dict[str, str]) -> None:
    """Give each named category this pcat id on the runtime registry (undone after the test)."""
    for name, pcat in pcats.items():
        monkeypatch.setitem(categories._BY_NAME, name, replace(categories._BY_NAME[name], pcat_id=pcat))


def with_handle(row: tuple) -> tuple:
    """An attributes.csv row (kind, value, ...) with its trailing `handle` cell: the label's handle for a category
    row, '' for any other."""
    return (*row, HANDLE_OF_LABEL[row[1]] if row[0] == "category" else "")
