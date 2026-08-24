"""Spine integration: polonine runs Stage 0 to Stage 5 via the orchestrator and
produces the expected artifacts, with determinism (a second run is identical).
"""

from __future__ import annotations

import glob

from stone_pipeline.config.settings import SETTINGS, category
from stone_pipeline.io import staging
from stone_pipeline.run import run_source


def test_polonine_spine_runs_and_is_deterministic(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    manifest_a = run_source("polonine", outputs_dir=out, state_dir=out)
    assert manifest_a.health_status == "OK"
    assert manifest_a.totals["rows"] == 303
    assert manifest_a.totals["variation_resolved"] >= 10
    assert manifest_a.gap_kind_counts  # gaps are recorded, not swallowed

    parquet = glob.glob(str(out / "**" / "canonical.parquet"), recursive=True)[0]
    rows_a = staging.read_canonical(parquet)
    emitted_a = (glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0])
    csv_a = open(emitted_a, encoding="utf-8").read()

    # Determinism: the emitted import CSV is byte-identical on a re-run (section
    # 11, 14A). The internal provenance method may shift from a fuzzy tier to an
    # exact-alias hit because write-back deliberately augments the reference, but
    # the product identity it emits does not change.
    run_source("polonine", outputs_dir=out, state_dir=out)
    csv_b = open(glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0], encoding="utf-8").read()
    assert csv_a == csv_b
    # the resolved variation ids are stable across the re-run too
    rows_b = staging.read_canonical(parquet)
    ids_a = sorted((r.surrogate_key, r.variation_id) for r in rows_a if r.variation_id)
    ids_b = sorted((r.surrogate_key, r.variation_id) for r in rows_b if r.variation_id)
    assert ids_a == ids_b

    # every resolved row carries provenance; no resolved variation is unexplained
    for row in rows_a:
        if row.variation_id:
            assert row.variation_method
            assert row.type_id is not None
            assert row.category_pcat_id == category("slab").pcat_id


def test_no_value_below_floor_reaches_ids(tmp_path):
    """Property check (section 14A): a null/unresolved attribute never ships a
    guessed id; an unresolved attribute leaves the id null and flags."""
    out = tmp_path / "outputs"
    out.mkdir()
    run_source("polonine", outputs_dir=out, state_dir=out)
    parquet = glob.glob(str(out / "**" / "canonical.parquet"), recursive=True)[0]
    for row in staging.read_canonical(parquet):
        # a resolved id must correspond to a resolved name
        if row.color_id is not None:
            assert row.color_name
        if row.finish_id is not None:
            assert row.finish_name
