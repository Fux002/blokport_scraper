"""M2 DoD: a healthy file passes and updates the baseline; a column-dropped file
FAILS with a localized diagnosis. Also exercises the fill-drop and row-count
collapse paths and the smoke-test hook.
"""

from __future__ import annotations

import polars as pl
import pytest

from stone_pipeline.config import contracts
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.stages import health


def _load_polonine() -> pl.DataFrame:
    frame = pl.read_csv(
        SETTINGS.paths.tests_fixtures_dir / "polonine_products_20260619_214426.csv",
        infer_schema_length=0,
    )
    return frame.rename({frame.columns[0]: frame.columns[0].lstrip("﻿")})


@pytest.fixture(scope="module")
def contract() -> contracts.SourceContract:
    c = contracts.load_contract("polonine")
    assert c is not None, "polonine contract must be generated first"
    return c


def test_healthy_file_passes_and_learns_baseline(contract, tmp_path):
    frame = _load_polonine()
    report = health.run_health(frame, contract, baseline=None)
    assert report.status == health.OK, report.messages
    learned = health.update_baseline_if_ok(
        report, contract, health.now_iso(), path=tmp_path / "baselines.json"
    )
    assert learned is not None
    assert learned.row_count == frame.height
    assert "material" in learned.fill_rates


def test_dropped_required_column_fails_with_diagnosis(contract):
    frame = _load_polonine().drop("material")
    report = health.run_health(frame, contract, baseline=None)
    assert report.status == health.FAILED
    missing = [d for d in report.drift if d.kind == "missing_column" and d.field == "material"]
    assert missing, "expected a missing_column diagnosis for material"


def test_renamed_column_is_localized(contract):
    # Simulate a site renaming material -> material_name (header drift).
    frame = _load_polonine().rename({"material": "material_name"})
    report = health.run_health(frame, contract, baseline=None)
    assert report.status == health.FAILED
    item = next(d for d in report.drift if d.field == "material")
    # The renamed column should be flagged as the likely replacement.
    assert item.likely_rename == "material_name"


def test_row_count_collapse_fails(contract):
    frame = _load_polonine().head(10)
    base = contracts.Baseline(source="polonine", row_count=303, fill_rates={})
    report = health.run_health(frame, contract, baseline=base)
    assert report.status == health.FAILED
    assert any(d.kind == "rowcount_collapse" for d in report.drift)


def test_fill_drop_on_required_field_fails(contract):
    frame = _load_polonine()
    # Blank out material entirely -> fill drops from ~1.0 to 0.
    frame = frame.with_columns(pl.lit("").alias("material"))
    base = contracts.Baseline(
        source="polonine", row_count=303, fill_rates={"material": 1.0}
    )
    report = health.run_health(frame, contract, baseline=base)
    assert report.status == health.FAILED
    assert any(d.kind == "fill_drop" and d.field == "material" for d in report.drift)


def test_smoke_test_failure_fails(contract):
    frame = _load_polonine()

    def broken_adapter(_sample: pl.DataFrame) -> int:
        raise ValueError("adapter parse error")

    report = health.run_health(frame, contract, baseline=None, smoke_test=broken_adapter)
    assert report.status == health.FAILED
    assert any(d.kind == "parse_fail" for d in report.drift)
