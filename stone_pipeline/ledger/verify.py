"""Verify the shadow ledger reproduces the to_upload CSVs (the Phase 1 cutover gate,
runnable on demand after a write-through run).

For each artifact, render it from the ledger and compare to the correct CSV:
  - variants:     by content set vs 1_variants_full.csv (populated by record_catalog)
  - products:     by content set, per source, vs 3_products_<source>.csv
  - combinations: byte-identical to 2_valid_combinations.csv (sorted both sides)
Variants and products are set (not byte) comparisons: a persistent ledger keeps each
row's original order, so once content matches but order drifts across runs (a reordered
listing, or export rows merged in) a byte compare would false-fail. Combinations are
byte-identical because write_combinations sorts both sides.

    python -m stone_pipeline.ledger.verify

Exits non-zero on any mismatch, so it can gate a build. No em dashes (design
principle 2).
"""

from __future__ import annotations

import csv
import filecmp
import tempfile
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core import logfmt
from stone_pipeline.ledger import writethrough
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.render import render_combinations, render_products, render_variants_full
from stone_pipeline.stages import tree_build

log = logfmt.get_logger("ledger.verify")


def _sources_with_products(to_upload: Path) -> list[str]:
    return sorted(p.name[len("3_products_"):-len(".csv")]
                  for p in to_upload.glob("3_products_*.csv")
                  if p.name != "3_products_all.csv")


def verify_products(ledger: Ledger, to_upload: Path) -> list[str]:
    """Per source, render its products from the ledger and compare to 3_products_<source>.csv
    by CONTENT (a set of rows). Set, not byte: a SKU keeps its original ledger rowid, so once
    a supplier reorders its listing the ledger order no longer tracks the file order even though
    every row matches (within a single run it is byte-identical; see the write-through test)."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for source in _sources_with_products(to_upload):
            correct = to_upload / f"3_products_{source}.csv"
            cfg = load_source(source)
            out = Path(tmp) / f"3_products_{source}.csv"
            render_products(ledger, cfg, out, source=cfg.source_code)
            rendered, expected = _rowset(out), _rowset(correct)
            if rendered != expected:
                problems.append(f"products mismatch for source '{source}' "
                                f"({len(rendered - expected)} only in ledger, "
                                f"{len(expected - rendered)} only in csv)")
    return problems


def _rowset(path: Path) -> set:
    with path.open(encoding="utf-8-sig", newline="") as h:
        rows = list(csv.reader(h))
    return {tuple(r) for r in rows[1:] if r}


def verify_variants(ledger: Ledger, to_upload: Path) -> list[str]:
    """Render the produced variant set (in_full rows) from the ledger and compare to
    1_variants_full.csv by CONTENT (a set of rows). Set, not byte: the integrated
    ledger merges export-seeded rows with the produced file, so row order differs from
    the file even when every row matches."""
    correct = to_upload / "1_variants_full.csv"
    if not correct.exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "1_variants_full.csv"
        render_variants_full(ledger, out)
        rendered, expected = _rowset(out), _rowset(correct)
        if rendered != expected:
            return [f"variants mismatch vs 1_variants_full.csv "
                    f"({len(rendered - expected)} only in ledger, "
                    f"{len(expected - rendered)} only in csv)"]
    return []


def verify_combinations(ledger: Ledger, to_upload: Path) -> list[str]:
    """Render the full combination set from the ledger and assert byte-identity with
    2_valid_combinations.csv. Heavy (the whole set); call when that file exists."""
    correct = to_upload / "2_valid_combinations.csv"
    if not correct.exists():
        return []
    products = to_upload / "3_products_all.csv"
    uncovered = SETTINGS.paths.review_dir / "tree_uncovered_variations.csv"
    assigned = tree_build._load_assigned_types(uncovered,
                                               tree_build._load_attributes(SETTINGS.paths.attributes_csv))
    delete_file = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    exclude_ids: set[str] = set()
    if delete_file.exists():
        with delete_file.open(encoding="utf-8-sig") as h:
            exclude_ids = {(r.get("Id") or "").strip()
                           for r in csv.DictReader(h) if (r.get("Id") or "").strip()}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "2_valid_combinations.csv"
        render_combinations(ledger, out, products_csv=products if products.exists() else None,
                            assigned_types=assigned, exclude_ids=exclude_ids)
        if not filecmp.cmp(str(out), str(correct), shallow=False):
            return ["combinations mismatch vs 2_valid_combinations.csv"]
    return []


def verify(ledger_path: Path | None = None, to_upload: Path | None = None,
           combinations: bool = True) -> list[str]:
    """Open the ledger and check every renderable artifact against the to_upload CSVs.
    Returns the list of problems (empty == the ledger matches the CSVs)."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    path = Path(ledger_path or writethrough.ledger_path())
    if not path.exists():
        return [f"no ledger at {path} (run with BLOKPORT_LEDGER_WRITETHROUGH=1 first)"]
    problems: list[str] = []
    with Ledger.open(path, env=writethrough.ENV_NAME) as ledger:
        problems += verify_variants(ledger, to_upload)
        problems += verify_products(ledger, to_upload)
        if combinations:
            problems += verify_combinations(ledger, to_upload)
    return problems


def main(argv: list[str] | None = None) -> int:
    problems = verify()
    if problems:
        for p in problems:
            log.error("ledger verify FAILED", extra={"extra_fields": {"problem": p}})
        print("ledger verify FAILED:\n  - " + "\n  - ".join(problems))
        return 1
    print("ledger verify OK: the ledger reproduces the to_upload CSVs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
