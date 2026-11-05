"""Golden 'row disposition' checks: a representative scraper FIXTURE row flows through the offline data
gates to its expected listing outcome. This ties the adapter's extraction (already golden in certify's
selftest) to the downstream handling, so a regression in EITHER -- the scraper dropping/misplacing a field,
or a stage mis-handling it -- fails in CI. Offline (no from_medusa export needed), so it runs on every push.

Extend with one assertion per representative hard row as fixtures gain them (stock word, block/MULTI,
colour, type-in-name). The identity/colour side (match + curate) needs the live export and is covered by the
controlled-index tests in test_colour_block.py and the mint tests instead.
"""

from __future__ import annotations

from stone_pipeline.adapters import REGISTRY
from stone_pipeline.adapters.base import read_scrape_csv
from stone_pipeline.adapters.selftest import fixture_dir
from stone_pipeline.stages import derive


def _adapt(source: str):
    return REGISTRY[source].adapt(read_scrape_csv(fixture_dir(source) / "input.csv"))


def test_marenostone_unlimited_stock_row_is_made_to_order():
    # marenostone flags made-to-order tiles as 'Unlimited' (a word, not a count). The row must EXTRACT that
    # into raw_stock_m2 (also golden in certify) and then list as made to order: on sale, no count, never an
    # invented number and never held.
    row = next(r for r in _adapt("marenostone") if r.raw_name == "Basalt Grey Tile")
    assert row.raw_stock_m2 == "Unlimited"                       # extraction
    derive.derive_inventory(row)                                 # handling
    assert row.made_to_order
    assert row.inventory_quantity is None
    assert row.inventory_method == "made_to_order_word"
