"""M0 DoD: a no-op run writes an empty manifest and a parquet round-trips.

Also exercises a non-empty round-trip with nested flags and gaps so the JSON
encoding in io/staging is proven, not just the empty case.
"""

from __future__ import annotations

import json

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core import ids
from stone_pipeline.core.schema import (
    CanonicalRow,
    FlagCode,
    GapKind,
    ReviewFlag,
    TreeGap,
)
from stone_pipeline.core.manifest import Manifest, StageMetric
from stone_pipeline.io import staging


def test_empty_parquet_round_trips(tmp_path):
    path = tmp_path / "empty.parquet"
    staging.write_canonical([], path)
    assert staging.read_canonical(path) == []


def test_nonempty_round_trip_preserves_nested(tmp_path):
    row = CanonicalRow(
        src_site="polonine",
        src_natural_key="620",
        surrogate_key="620",
        raw_name="ALPINE",
        raw_image_urls=["http://x/1.jpg", "http://x/2.jpg"],
        port_ids=["p1", "p2"],
        review_flags=[
            ReviewFlag(
                field="quality",
                code=FlagCode.attr_unresolved,
                raw_value="First",
                confidence=Confidence.low,
                method="synonym",
            )
        ],
        tree_gaps=[
            TreeGap(
                src_site="polonine",
                surrogate_key="620",
                raw_name="ALPINE",
                gap_kind=GapKind.missing_variation,
            )
        ],
    )
    path = tmp_path / "one.parquet"
    staging.write_canonical([row], path)
    back = staging.read_canonical(path)
    assert len(back) == 1
    got = back[0]
    assert got.raw_image_urls == ["http://x/1.jpg", "http://x/2.jpg"]
    assert got.port_ids == ["p1", "p2"]
    assert got.review_flags[0].code == FlagCode.attr_unresolved.value
    assert got.tree_gaps[0].gap_kind == GapKind.missing_variation.value
    assert got == row


def test_manifest_serializes_cleanly(tmp_path):
    manifest = Manifest(run_id="develi_20260101_000000", source="develi",
                        code_version="0.1.0", environment="dev-staging")
    manifest.add_stage(StageMetric(stage="ingest", rows_in=10, rows_out=9))
    path = manifest.write(tmp_path / "m.json")
    payload = json.loads(path.read_text())
    assert payload["run_id"] == "develi_20260101_000000"
    assert payload["stage_metrics"][0]["stage"] == "ingest"


def test_determinism_helpers_are_stable():
    a = ids.mint_surrogate("polonine", "http://x/p", "ALPINE", 3)
    b = ids.mint_surrogate("polonine", "http://x/p", "ALPINE", 3)
    assert a == b and a.startswith("mint_")
    assert ids.seeded_uniform("k", "weight", 0.225, 0.350) == ids.seeded_uniform(
        "k", "weight", 0.225, 0.350
    )
