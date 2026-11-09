"""Golden pin: run_source on the committed fixtures produces byte-identical outputs.

Six scenarios, each with ledger write-through on a seeded tmp ledger. The first three and the two guard scenarios
run both fixture sources; the lot scenario runs the lot source:
  full_cold     FULL mode, no Medusa product export (everything is new)
  full_known    FULL mode against an export (half the emitted SKUs existing, two live SKUs the scrape dropped, one
                sold): the new/existing split, the stock delta and the delist of the dropped SKUs
  stock         the stock-only refresh against the same export
  lots          FULL mode on the lot source (its adapter fixture: one bundle whose every slab is measured and in
                stock) against an export holding every bundle: the bundle becomes its lot, so its SKU leaves the
                scrape and its delist is a replacement (the lot replacement CSV, the discontinued action naming the
                lot, the ledger's replaced_by_lot inventory row)
  mass_delist   FULL mode against an export in which half of each source's live SKUs are gone: the mass-delist
                guard refuses every delist (logged, nothing discontinued) and the run goes on
  detail_floor  FULL mode on the fixtures, every scrape completion marker reporting detail fetch failures at the
                floor, against the full_known export: the detail-failure guard refuses marenostone's delist the
                same way, and polonine (its fixture predates the marker, so no failure is on record) still delists
The scenarios that make a guard or the lot replacement fire also assert it fired, so a scenario that silently stops
reaching its path fails by name rather than only as a changed digest.
Every output is digested: each file under the outputs and state folders (a parquet by its decoded rows and schema, so a
polars upgrade cannot fail it), every ledger table (rows sorted, wall-clock timestamps masked), the magnitude baselines
and the returned manifests. The only volatile bytes are wall-clock timestamps (ledger columns and the two baselines'
updated_at); everything else is deterministic. The digests are compared with GOLDEN, written from the code before the
C2 split of run_source; a mismatch names every output that changed.

Regenerate (only for an INTENDED output change): RUN_SOURCE_GOLDEN=rewrite pytest stone_pipeline/tests/test_run_source_golden.py
(on CI: dispatch the "Golden rewrite" workflow with the branch, download its artifact, review the diff, commit it)
Any other value is refused, and a rewrite always fails the run, so a regenerated golden is never a silent pass."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from stone_pipeline import run as run_module
from stone_pipeline.config import contracts
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.run import OutputMode, run_source
from stone_pipeline.stages import images, magnitude_drift
from stone_pipeline.stages.product_state import sku_from
from stone_pipeline.tests.test_ledger_writethrough import _seed_ledger_like_the_sync_server

GOLDEN = Path(__file__).parent / "fixtures" / "run_source_golden.json"
REWRITE_VALUE = "rewrite"
SOURCES = ("polonine", "marenostone")
_TIMESTAMP = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")
# The lot scenario's scrape is the lot source's adapter fixture: bundle 2555 (B6208B) has nine scanned slabs, each
# measured, against a stock of nine, so the loader turns it into its lot. Laid out as a scrape folder named by the
# rows' own scrape timestamp.
LOT_SOURCE = "sentas"
LOT_SCRAPE = Path(__file__).resolve().parents[1] / "adapters" / "fixtures" / LOT_SOURCE / "input.csv"
LOT_SCRAPE_FOLDER = "20261009_172539"
LOT_BUNDLE = "2555"
MASS_REFUSED = "delist refused: a single run would discontinue too much"
DETAIL_REFUSED = "delist refused: the scrape's detail fetches were too degraded"


def _stock_run(source, out, data_dir, known):
    return run_source(source, outputs_dir=out, state_dir=out, data_dir=data_dir, known_products_path=known,
                      mode=OutputMode.STOCK)


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
    return _write_export(path, rows)


def _mass_delist_export(path: Path, emitted_csvs: list[Path]) -> Path:
    """Per source, half of the SKUs the cold run emitted existing, and as many live SKUs again that the scrape no
    longer carries: half of the source's live catalogue would be delisted, over the mass-delist guard's fraction."""
    rows = []
    for emitted in emitted_csvs:
        with emitted.open(encoding="utf-8-sig", newline="") as h:
            skus = sorted({r["Variant Sku"] for r in csv.DictReader(h)})
        existing = skus[: len(skus) // 2]
        code = load_source(emitted.parent.parent.name.rsplit("_", 2)[0]).source_code.upper()
        rows += [(sku, 999) for sku in existing] + [(f"{code}-GOLDEN-MASS-{i}", 5) for i in range(len(existing))]
    return _write_export(path, rows)


def _lot_export(path: Path) -> Path:
    """Every bundle of the lot scrape live in Medusa: the one that becomes its lot is no longer in the scrape."""
    code = load_source(LOT_SOURCE).source_code
    with LOT_SCRAPE.open(encoding="utf-8-sig", newline="") as h:
        return _write_export(path, [(sku_from(code, r["product_id"]), 3) for r in csv.DictReader(h)])


def _write_export(path: Path, rows: list[tuple[str, int]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as h:
        writer = csv.writer(h)
        writer.writerow(["Product Handle", "Variant Sku", "Inventory Quantity"])
        writer.writerows([(f"h-{sku.lower()}", sku, qty) for sku, qty in rows])
    return path


def _lot_data_dir(base: Path, monkeypatch) -> Path:
    """The lot source's scrape folder from its adapter fixture. The fixture is a slice of a source that scrapes about
    500 records, so the source's row floor and its contract's row baseline are set to the slice: either would (rightly)
    abort a real scrape this short."""
    scrape = base / LOT_SOURCE / LOT_SCRAPE_FOLDER
    scrape.mkdir(parents=True)
    shutil.copyfile(LOT_SCRAPE, scrape / "products.csv")
    with LOT_SCRAPE.open(encoding="utf-8-sig", newline="") as h:
        records = sum(1 for _ in csv.DictReader(h))
    (scrape / "scrape_complete.json").write_text(json.dumps(
        {"rows": records, "complete": True, "timestamp": LOT_SCRAPE_FOLDER}), encoding="utf-8")
    cfg = dataclasses.replace(load_source(LOT_SOURCE), min_expected_rows=records)
    contract = dataclasses.replace(contracts.load_contract(LOT_SOURCE), row_baseline=records)
    real_source, real_contract = run_module.load_source, contracts.load_contract
    monkeypatch.setattr(run_module, "load_source", lambda s: cfg if s == LOT_SOURCE else real_source(s))
    monkeypatch.setattr(contracts, "load_contract",
                        lambda s, path=None: contract if s == LOT_SOURCE else real_contract(s, path))
    return base


def _degraded_data_dir(base: Path, data_dir: Path) -> tuple[Path, set[str]]:
    """The fixture scrapes, each completion marker reporting its detail fetches failed at exactly the delist floor;
    with the sources that have a marker (a scrape folder without one records no detail failures)."""
    shutil.copytree(data_dir, base)
    degraded = set()
    for marker in sorted(base.glob("*/*/scrape_complete.json")):
        body = json.loads(marker.read_text(encoding="utf-8"))
        body["detail_failure_ratio"] = SETTINGS.thresholds.delist_detail_failure_floor
        marker.write_text(json.dumps(body), encoding="utf-8")
        degraded.add(marker.parent.parent.name)
    return base, degraded


def _discontinued(out: Path, source: str) -> list[dict]:
    (path,) = out.glob(f"{source}_*/review/products_discontinued.csv")
    with path.open(encoding="utf-8-sig", newline="") as h:
        return list(csv.DictReader(h))


def _assert_delist_refused(out: Path, caplog, refused: str, other: str, sources) -> None:
    """The delist of each of `sources` was refused by `refused` (logged once per source, nothing discontinued), and
    every other source still delisted; `other` never refused anything."""
    messages = [r.getMessage() for r in caplog.records]
    assert sum(refused in m for m in messages) == len(sources)
    assert not any(other in m for m in messages)
    assert {source: _discontinued(out, source) == [] for source in SOURCES} == {s: s in sources for s in SOURCES}


def _assert_lot_replaced(base: Path) -> None:
    """The bundle's delist names its lot: the replacement CSV, the discontinued action, the ledger inventory row."""
    out, sku = base / "out", sku_from(load_source(LOT_SOURCE).source_code, LOT_BUNDLE)
    (path,) = out.glob(f"{LOT_SOURCE}_*/review/lot_replacements.csv")
    with path.open(encoding="utf-8-sig", newline="") as h:
        replaced = list(csv.DictReader(h))
    assert [(r["Variant Sku"], r["Pieces"]) for r in replaced] == [(sku, "9")]
    lot = replaced[0]["Lot Code"]
    assert [lot in r["Action"] for r in _discontinued(out, LOT_SOURCE)] == [True]
    con = sqlite3.connect(base / "scenario.ledger")
    try:
        row = con.execute("SELECT qty, reason, replaced_by_lot_code FROM inventory WHERE sku = ?", (sku,)).fetchone()
    finally:
        con.close()
    assert row == (0, "replaced_by_lot", lot)


def _scenario(base: Path, monkeypatch, run, sources=SOURCES) -> dict[str, str]:
    """Run `run(source, out)` for each source into base/out with write-through on base's own ledger; digest it all."""
    base.mkdir(parents=True)
    ledger_path = base / "scenario.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(images, "published_keys", lambda *a, **k: frozenset())   # the fixtures publish no photo
    _seed_ledger_like_the_sync_server(ledger_path, base)
    out = base / "out"
    digests = {}
    for source in sources:
        manifest = run(source, out)
        digests[f"MANIFEST:{source}"] = _sha(json.dumps(manifest.to_dict(), sort_keys=True, default=str).encode())
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        digests[str(path.relative_to(out))] = _file_digest(path)
    digests.update(_ledger_digests(ledger_path))
    digests["magnitude_baselines"] = _file_digest(magnitude_drift._path())
    return digests


def _full_run(data_dir, known=None):
    return lambda s, out: run_source(s, outputs_dir=out, state_dir=out, data_dir=data_dir, known_products_path=known)


def _collect(tmp_path, monkeypatch, caplog, data_dir) -> dict[str, str]:
    got = {}
    cold = _scenario(tmp_path / "full_cold", monkeypatch, _full_run(data_dir))
    got.update({f"full_cold/{k}": v for k, v in cold.items()})
    emitted = sorted((tmp_path / "full_cold" / "out").glob("*/4_products_import/medusa_import.csv"))
    known = _known_export(tmp_path / "products_export.csv", emitted)
    full = _scenario(tmp_path / "full_known", monkeypatch, _full_run(data_dir, known))
    got.update({f"full_known/{k}": v for k, v in full.items()})
    stock = _scenario(tmp_path / "stock", monkeypatch, lambda s, out: _stock_run(s, out, data_dir, known))
    got.update({f"stock/{k}": v for k, v in stock.items()})
    # the scenarios that make the lot replacement and each delist guard fire (after the three above: the magnitude
    # baselines they digest are shared across the scenarios)
    lot_data = _lot_data_dir(tmp_path / "lot_data", monkeypatch)
    lot = _scenario(tmp_path / "lots", monkeypatch, _full_run(lot_data, _lot_export(tmp_path / "lot_export.csv")),
                    sources=(LOT_SOURCE,))
    _assert_lot_replaced(tmp_path / "lots")
    got.update({f"lots/{k}": v for k, v in lot.items()})
    caplog.clear()
    mass = _scenario(tmp_path / "mass_delist", monkeypatch,
                     _full_run(data_dir, _mass_delist_export(tmp_path / "mass_export.csv", emitted)))
    _assert_delist_refused(tmp_path / "mass_delist" / "out", caplog, MASS_REFUSED, DETAIL_REFUSED, set(SOURCES))
    got.update({f"mass_delist/{k}": v for k, v in mass.items()})
    caplog.clear()
    degraded_data, degraded = _degraded_data_dir(tmp_path / "degraded_data", data_dir)
    assert degraded == {"marenostone"}                        # the fixture scrapes that carry a completion marker
    detail = _scenario(tmp_path / "detail_floor", monkeypatch, _full_run(degraded_data, known))
    _assert_delist_refused(tmp_path / "detail_floor" / "out", caplog, DETAIL_REFUSED, MASS_REFUSED, degraded)
    got.update({f"detail_floor/{k}": v for k, v in detail.items()})
    return got


def test_run_source_outputs_match_the_golden(tmp_path, monkeypatch, caplog, scrape_data_dir):
    got = _collect(tmp_path, monkeypatch, caplog, scrape_data_dir)
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
