"""M3 DoD: the polonine adapter fixture self-test passes. Also asserts the
adapter framework invariants: row isolation, the variety_match_key declaration,
and that the adapter does not build downstream fields (title/handle/ids).
"""

from __future__ import annotations

from stone_pipeline.adapters import selftest
from stone_pipeline.adapters.base import read_scrape_csv
from stone_pipeline.adapters.polonine import ADAPTER as POLONINE
from stone_pipeline.config.settings import SETTINGS


def test_polonine_fixture_selftest_passes():
    ok, message = selftest.run_fixture("polonine")
    assert ok, message


def test_polonine_full_scrape_adapts_without_loss():
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv"
    )
    rows = POLONINE.adapt(frame)
    # near-complete: polonine has clean natural keys, so almost nothing drops
    assert len(rows) >= 0.98 * frame.height
    first = rows[0]
    assert first.src_site == "polonine"
    assert first.variety_match_key  # declared and populated
    # the adapter no longer asserts a format: the scrape has no explicit tag, so
    # raw_format is empty and the Format Resolver infers it downstream
    assert first.raw_format == ""
    assert POLONINE.format_field == "format"  # the declared scraper tag column
    # adapter must NOT populate downstream fields
    assert first.title is None
    assert first.handle is None
    assert first.variation_id is None
    assert first.type_id is None


def test_adapter_isolates_bad_rows():
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv"
    )
    # blank the natural key on a few rows; they should drop, not crash the batch
    import polars as pl

    mutated = frame.with_columns(
        pl.when(pl.int_range(pl.len()) < 3)
        .then(pl.lit(""))
        .otherwise(pl.col("product_id"))
        .alias("product_id")
    )
    rows = POLONINE.adapt(mutated)
    assert len(rows) == frame.height - 3


def test_adapter_carries_fetch_failed_signal_with_zero_per_adapter_work():
    # the scraper's reserved `fetch_failed` column auto-maps to CanonicalRow.fetch_failed_fields for EVERY
    # adapter (no field_map entry), so "hold, never default a fetch-failed dimension" works everywhere.
    import polars as pl
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv"
    )
    assert all(r.fetch_failed_fields == [] for r in POLONINE.adapt(frame))   # absent column -> []
    marked = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 0).then(pl.lit("dims")).otherwise(pl.lit("")).alias("fetch_failed")
    )
    rows = POLONINE.adapt(marked)
    assert rows[0].fetch_failed_fields == ["dims"]                # the marked row carries it
    assert all(r.fetch_failed_fields == [] for r in rows[1:])     # others stay empty


def test_contract_generated_from_adapter_matches_required():
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv"
    )
    contract = POLONINE.generate_contract(frame)
    assert contract.required_columns == POLONINE.required_columns
    assert contract.adapter_version == POLONINE.adapter_version
