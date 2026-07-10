"""Phase 1 layer diagnostics: every stage returns a uniform StageMetric with a status on the shared
OK/DEGRADED/FAILED ladder, and the run writes a per-layer stages.json. Diagnostics are observers -- they
never change routing (the emit-determinism guard lives in test_spine_end_to_end).
"""

from __future__ import annotations

import glob
import json

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.manifest import StageMetric
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.run import run_source
from stone_pipeline.stages import constants, derive, match_variation, normalize


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


# --- StageMetric shape --------------------------------------------------------
def test_stage_metric_summary_and_status_default():
    m = StageMetric(stage="x", rows_in=5, rows_out=4, reviewed=1, extra={"k": 2})
    assert m.status == "OK"                               # default
    assert m.summary() == "x: OK (5->4, 1 reviewed) [k=2]"
    assert StageMetric(stage="y", status="DEGRADED").summary().startswith("y: DEGRADED")


# --- normalize ----------------------------------------------------------------
def test_normalize_report_ok_on_resolvable(ref):
    rows = [CanonicalRow(src_site="polonine", raw_color="Black") for _ in range(4)]
    m = normalize.run(rows, ref)
    assert m.stage == "normalize" and m.status == "OK"
    assert m.rows_in == 4 and m.extra["unresolved_rows"] == 0


def test_normalize_report_degraded_on_unresolved(ref):
    rows = [CanonicalRow(src_site="x", raw_color="Chartreuse Sparkle") for _ in range(4)]
    m = normalize.run(rows, ref)
    assert m.status == "DEGRADED"                         # 4/4 unresolved >= threshold
    assert m.extra["unresolved_rows"] == 4


# --- match_variation ----------------------------------------------------------
def test_match_report_degraded_when_all_unmatched(ref):
    rows = [CanonicalRow(src_site="x", raw_format="Slab",
                         variety_match_key=f"Zzxq Nonexistent {i}") for i in range(3)]
    m = match_variation.run(rows, ref)
    assert m.stage == "match_variation" and m.status == "DEGRADED"
    assert m.extra["resolved"] == 0 and m.gapped == 3


# --- derive -------------------------------------------------------------------
def test_derive_report_ok_with_dimensions(ref):
    cfg = load_source("polonine")
    rows = [CanonicalRow(src_site="polonine", raw_name="Test Slab", raw_format="Slab",
                         raw_dimensions="length=2.5;height=1.5", raw_thickness="0.02") for _ in range(4)]
    m = derive.run(rows, ref, cfg)
    assert m.stage == "derive" and m.status == "OK"
    assert m.extra["incomplete_dims"] == 0 and m.extra["provenance_gaps"] == 0


def test_derive_report_degraded_without_dimensions(ref):
    cfg = load_source("polonine")
    rows = [CanonicalRow(src_site="polonine", raw_name="No Dims", raw_format="Slab") for _ in range(4)]
    m = derive.run(rows, ref, cfg)
    assert m.status == "DEGRADED" and m.extra["incomplete_dims"] == 4


# --- constants ----------------------------------------------------------------
def test_constants_report_shape(ref):
    cfg = load_source("polonine")
    rows = [CanonicalRow(src_site="polonine", raw_name="A") for _ in range(2)]
    m = constants.run(rows, cfg)
    assert m.stage == "constants" and m.rows_out == 2 and "missing_owner" in m.extra


# --- integration: stages.json + per-stage metrics on a real run ---------------
def test_run_writes_per_stage_diagnostics(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    manifest = run_source("polonine", outputs_dir=out, state_dir=out)
    recorded = {m.stage for m in manifest.stage_metrics}
    # the four previously-silent stages + images now report, alongside the pre-existing ones
    assert {"normalize", "match_variation", "derive", "constants", "images"} <= recorded

    stages_path = glob.glob(str(out / "**" / "stages.json"), recursive=True)[0]
    doc = json.loads(open(stages_path, encoding="utf-8").read())
    by_stage = {s["stage"]: s for s in doc["stages"]}
    assert {"normalize", "match_variation", "derive", "constants", "images"} <= set(by_stage)
    assert doc["health"] == "OK"
    for s in doc["stages"]:
        assert s["status"] in ("OK", "DEGRADED", "FAILED", "skipped", "not_run")
