"""M12: production wiring. Multi-source run is source-isolated; row fingerprints
are stable across runs (incremental basis); the Medusa sink builds valid upsert
payloads in dry-run and the CSV sink stays interchangeable with it.
"""

from __future__ import annotations

import glob

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io.medusa_client import CsvImportSink, MedusaApiSink
from stone_pipeline.io import staging
from stone_pipeline.run import run_all, run_source


def test_multi_source_isolation(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    results = run_all(["polonine", "marenostone"], outputs_dir=out, state_dir=out)
    assert set(results) == {"polonine", "marenostone"}
    assert all(m.health_status in ("OK", "DEGRADED") for m in results.values())


def test_row_fingerprint_stable_across_runs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    run_source("polonine", outputs_dir=out, state_dir=out)
    parquet = glob.glob(str(out / "**" / "canonical.parquet"), recursive=True)[0]
    a = {r.surrogate_key: r.row_fingerprint for r in staging.read_canonical(parquet)}
    run_source("polonine", outputs_dir=out, state_dir=out)
    b = {r.surrogate_key: r.row_fingerprint for r in staging.read_canonical(parquet)}
    assert a == b
    assert all(v for v in a.values())  # every row has a fingerprint


def test_medusa_sink_builds_payloads_dry_run():
    cfg = load_source("polonine")
    row = CanonicalRow(src_site="polonine", surrogate_key="620", handle="x-pol-620",
                       title="Verde Ubatuba Polished Slab", variation_id="v", type_id="t",
                       color_id="c", finish_id="f", quality_id="q", category_pcat_id="pcat",
                       bundle_size=9, image_keys=["http://img/1.jpg"])
    sink = MedusaApiSink(dry_run=True)
    # dry-run returns 0 but must construct payloads without error
    assert sink.send([row], cfg) == 0
    payload = sink._payload(row, cfg)
    assert payload["handle"] == "x-pol-620"
    assert payload["variants"][0]["sku"] == "POL-620"
    assert payload["metadata"]["stn_variation_id"] == "v"


def test_csv_and_api_sinks_share_interface(tmp_path):
    cfg = load_source("polonine")
    row = CanonicalRow(src_site="polonine", surrogate_key="620", handle="x-pol-620",
                       title="T", variation_id="v")
    csv_sink = CsvImportSink(tmp_path / "out.csv")
    api_sink = MedusaApiSink(dry_run=True)
    assert csv_sink.send([row], cfg) == 1  # wrote the row
    assert api_sink.send([row], cfg) == 0  # dry-run
    assert (tmp_path / "out.csv").exists()
