"""Golden pin: run_source on the committed fixtures produces byte-identical outputs.

Three scenarios over both fixture sources, each with ledger write-through on a seeded tmp ledger:
  full_cold    FULL mode, no Medusa product export (everything is new)
  full_known   FULL mode against an export (half the emitted SKUs existing, two live SKUs the scrape dropped, one
               sold): the new/existing split, the stock delta, the delist and the lot replacements
  stock        the stock-only refresh against the same export
Every output is digested: each file under the outputs and state folders (a parquet by its decoded rows and schema, so a
polars upgrade cannot fail it), every ledger table (rows sorted, wall-clock timestamps masked), the magnitude baselines
and the returned manifests. The only volatile bytes are wall-clock timestamps (ledger columns and the two baselines'
updated_at); everything else is deterministic. The digests are compared with GOLDEN, written from the code before the
C2 split of run_source; a mismatch names every output that changed.

Regenerate (only for an INTENDED output change): RUN_SOURCE_GOLDEN=rewrite pytest stone_pipeline/tests/test_run_source_golden.py
Any other value is refused, and a rewrite always fails the run, so a regenerated golden is never a silent pass."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.run import run_source
from stone_pipeline.stages import magnitude_drift
from stone_pipeline.tests.test_ledger_writethrough import _seed_ledger_like_the_sync_server

GOLDEN = Path(__file__).parent / "fixtures" / "run_source_golden.json"
REWRITE_VALUE = "rewrite"
SOURCES = ("polonine", "marenostone")
_TIMESTAMP = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")


def _stock_run(source, out, data_dir, known):
    return run_source(source, outputs_dir=out, state_dir=out, data_dir=data_dir, known_products_path=known,
                      inventory_only=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    if path.suffix == ".parquet":
        frame = pl.read_parquet(path)
        return _sha(json.dumps([str(frame.schema), frame.to_dicts()], sort_keys=True, default=str).encode())
    data = path.read_bytes()
    if path.name in ("scrape_baselines.json", "magnitude_baselines.json"):   # their updated_at is wall-clock
        data = _TIMESTAMP.sub(b"<ts>", data)
    return _sha(data)


def _ledger_digests(ledger_path: Path) -> dict[str, str]:
    con = sqlite3.connect(ledger_path)
    try:
        tables = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {f"LEDGER:{t}": _sha(_TIMESTAMP.sub(b"<ts>", "\n".join(
            sorted(repr(r) for r in con.execute(f"SELECT * FROM {t}"))).encode())) for t in tables}
    finally:
        con.close()


def _known_export(path: Path, emitted_csvs: list[Path]) -> Path:
    """Half of the SKUs the cold run emitted (sorted, deterministic) existing at a stock that moved, two live SKUs per
    source the scrape no longer carries (delisted, under the 30% guard) and one already sold (stock 0)."""
    rows = []
    for emitted in emitted_csvs:
        with emitted.open(encoding="utf-8-sig", newline="") as h:
            skus = sorted({r["Variant Sku"] for r in csv.DictReader(h)})
        rows += [(sku, 999) for sku in skus[: len(skus) // 2]]
    for source in SOURCES:
        code = load_source(source).source_code.upper()
        rows += [(f"{code}-GOLDEN-GONE-1", 5), (f"{code}-GOLDEN-GONE-2", 5), (f"{code}-GOLDEN-SOLD", 0)]
    with path.open("w", newline="", encoding="utf-8") as h:
        writer = csv.writer(h)
        writer.writerow(["Product Handle", "Variant Sku", "Inventory Quantity"])
        writer.writerows([(f"h-{sku.lower()}", sku, qty) for sku, qty in rows])
    return path


def _scenario(base: Path, monkeypatch, run) -> dict[str, str]:
    """Run `run(source, out)` for each source into base/out with write-through on base's own ledger; digest it all."""
    base.mkdir(parents=True)
    ledger_path = base / "scenario.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(ledger_path))
    _seed_ledger_like_the_sync_server(ledger_path, base)
    out = base / "out"
    digests = {}
    for source in SOURCES:
        manifest = run(source, out)
        digests[f"MANIFEST:{source}"] = _sha(json.dumps(manifest.to_dict(), sort_keys=True, default=str).encode())
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        digests[str(path.relative_to(out))] = _file_digest(path)
    digests.update(_ledger_digests(ledger_path))
    digests["magnitude_baselines"] = _file_digest(magnitude_drift._path())
    return digests


def _collect(tmp_path, monkeypatch, data_dir) -> dict[str, str]:
    got = {}
    cold = _scenario(tmp_path / "full_cold", monkeypatch,
                     lambda s, out: run_source(s, outputs_dir=out, state_dir=out, data_dir=data_dir))
    got.update({f"full_cold/{k}": v for k, v in cold.items()})
    known = _known_export(tmp_path / "products_export.csv",
                          sorted((tmp_path / "full_cold" / "out").glob("*/4_products_import/medusa_import.csv")))
    full = _scenario(tmp_path / "full_known", monkeypatch,
                     lambda s, out: run_source(s, outputs_dir=out, state_dir=out, data_dir=data_dir,
                                               known_products_path=known))
    got.update({f"full_known/{k}": v for k, v in full.items()})
    stock = _scenario(tmp_path / "stock", monkeypatch, lambda s, out: _stock_run(s, out, data_dir, known))
    got.update({f"stock/{k}": v for k, v in stock.items()})
    return got


def test_run_source_outputs_match_the_golden(tmp_path, monkeypatch, scrape_data_dir):
    got = _collect(tmp_path, monkeypatch, scrape_data_dir)
    mode = os.environ.get("RUN_SOURCE_GOLDEN")
    if mode is not None:
        if mode != REWRITE_VALUE:
            pytest.fail(f"RUN_SOURCE_GOLDEN={mode!r} is not honoured: only {REWRITE_VALUE!r} rewrites the golden")
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.fail(f"golden REWRITTEN ({len(got)} outputs) at {GOLDEN}; review the diff, then run again without the env var")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    changed = sorted(k for k in set(want) & set(got) if want[k] != got[k])
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    report = "\n".join([f"  changed: {k}" for k in changed] + [f"  no longer written: {k}" for k in missing]
                       + [f"  newly written: {k}" for k in extra])
    assert not (changed or missing or extra), f"run_source outputs differ from the golden:\n{report}"
