"""CanonicalRow.degraded was written by run (the health status copied onto every row) and read nowhere.
Dropping a row field is safe for staged parquet: the model ignores columns it no longer declares, so an
older source's staging still loads and no republish is needed."""

from __future__ import annotations

import polars as pl

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io import staging


def test_the_row_carries_no_degraded_flag():
    assert "degraded" not in CanonicalRow.model_fields


def test_a_staged_parquet_with_a_column_the_row_no_longer_has_still_loads(tmp_path):
    rows = [CanonicalRow(src_site="x", surrogate_key="1", raw_name="Alpha")]
    frame = staging.rows_to_frame(rows).with_columns(pl.lit(True).alias("degraded"))   # the old column
    path = tmp_path / "canonical.parquet"
    frame.write_parquet(path)
    loaded = staging.read_canonical(path)
    assert [r.raw_name for r in loaded] == ["Alpha"]
