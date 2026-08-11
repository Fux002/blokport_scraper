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


def test_marenostone_maps_structured_stock_status_to_canonical():
    # The scraper always reads the WooCommerce stock_status ("in-stock"/"out-of-stock"), but it only helps
    # if the ADAPTER carries it into the canonical row -- the exact miss that held ~24 in-stock travertines
    # (the signal died at the adapter boundary, invisible to every derive-level test). This proves the map
    # scraper -> canonical, so a dropped mapping fails here instead of silently in production.
    import polars as pl
    from stone_pipeline.adapters.marenostone import ADAPTER as MARENO
    frame = pl.DataFrame({
        "product_id": ["1", "2"],
        "name": ["Super White Travertine Slab", "Cloud White Marble Slab"],
        "attr_category1": ["Travertine", "Marble"], "attr_category2": ["White", "White"],
        "attr_format": ["slab", "slab"], "attr_finish": ["Polished", "Polished"],
        "image_urls": ["http://x/a.jpg", "http://x/b.jpg"],
        # the second value carries trailing THEME classes, as the live site emits for out-of-stock; the
        # adapter must keep only the availability keyword so the value is clean regardless of theme markup.
        "stock_status": ["in-stock", "out-of-stock wd-style-with-bg"],
    })
    rows = MARENO.adapt(frame)
    assert [r.raw_stock_status for r in rows] == ["in-stock", "out-of-stock"]


def test_build_dims_and_na_contract():
    # the ONE shared dimension-string builder (was copy-pasted per adapter). Locks the contract the 4
    # migrated adapters + every future one rely on.
    from stone_pipeline.adapters.base import AdapterBase as A
    assert A.build_dims("3.2", "2.0", unit="m") == "length=3.2m;height=2.0m"
    assert A.build_dims("250", "160") == "length=250;height=160"          # unit="" -> no suffix (marenostone)
    assert A.build_dims("", "") == ""                                     # both blank -> empty
    assert A.build_dims("3.2", "", unit="m") == "length=3.2m;height=m"    # single side keeps the shape
    assert A.build_dims("N/A", "160", unit="", blank_na=True) == "length=;height=160"   # N/A -> blank
    assert A.build_dims("N/A", "N/A", blank_na=True) == ""                # both N/A -> empty
    assert A.na("N/A") == "" and A.na(" x ") == "x" and A.na(None) == ""


def test_contract_generated_from_adapter_matches_required():
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv"
    )
    contract = POLONINE.generate_contract(frame)
    assert contract.required_columns == POLONINE.required_columns
    assert contract.adapter_version == POLONINE.adapter_version


def test_every_committed_contract_version_matches_its_adapter():
    # the committed source_contracts.yaml adapter_version must track the adapter's own adapter_version, so
    # the stamped provenance (health baseline / diagnostics) is never stale. The existing polonine test above
    # only checks a GENERATED contract (trivially matches); this guards the COMMITTED yaml against drift.
    from stone_pipeline.adapters import REGISTRY
    from stone_pipeline.config import contracts
    for name, adapter in REGISTRY.items():
        committed = contracts.load_contract(name)
        if committed is None:
            continue   # no committed contract -> health generates one from the adapter, trivially in sync
        assert committed.adapter_version == adapter.adapter_version, (
            f"source_contracts.yaml adapter_version for {name!r} is {committed.adapter_version} but the "
            f"adapter declares {adapter.adapter_version} -- bump the contract when you bump the adapter")
