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
from stone_pipeline.config.domain import active_pack
from stone_pipeline.stages import derive
from stone_pipeline.stages.derive import _dimension_category


def _adapt(source: str):
    return REGISTRY[source].adapt(read_scrape_csv(fixture_dir(source) / "input.csv"))


def test_marenostone_unlimited_stock_row_lists_via_the_in_stock_fallback():
    # marenostone flags made-to-order tiles as 'Unlimited' (a word, not a count). The row must EXTRACT that
    # into raw_stock_m2 (also golden in certify) and then LIST via the in-stock fallback, never be held.
    row = next(r for r in _adapt("marenostone") if r.raw_name == "Basalt Grey Tile")
    assert row.raw_stock_m2 == "Unlimited"                       # extraction
    derive.derive_inventory(row)                                 # handling
    # lists via the per-category made-to-order fallback (positive), never held as stock_undetermined
    assert row.inventory_quantity == active_pack().in_stock_fallback_qty[_dimension_category(row)]
    assert row.inventory_quantity > 0
    assert row.inventory_method == "in_stock_word_fallback"      # lists, not stock_undetermined (held)
