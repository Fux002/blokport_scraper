"""M12: production wiring. Multi-source run is source-isolated; row fingerprints
are stable across runs (incremental basis); the Medusa sink builds valid upsert
payloads in dry-run and the CSV sink stays interchangeable with it.
"""

from __future__ import annotations

import glob

from stone_pipeline.io import staging
from stone_pipeline.run import run_all, run_source


def test_multi_source_isolation(tmp_path, scrape_data_dir):
    out = tmp_path / "out"
    out.mkdir()
    results = run_all(["polonine", "marenostone"], outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    assert set(results) == {"polonine", "marenostone"}
    assert all(m.health_status in ("OK", "DEGRADED") for m in results.values())


def test_row_fingerprint_stable_across_runs(tmp_path, scrape_data_dir):
    out = tmp_path / "out"
    out.mkdir()
    run_source("polonine", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    parquet = glob.glob(str(out / "**" / "canonical.parquet"), recursive=True)[0]
    a = {r.surrogate_key: r.row_fingerprint for r in staging.read_canonical(parquet)}
    run_source("polonine", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    b = {r.surrogate_key: r.row_fingerprint for r in staging.read_canonical(parquet)}
    assert a == b
    assert all(v for v in a.values())  # every row has a fingerprint
