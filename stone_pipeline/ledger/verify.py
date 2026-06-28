"""Verify the shadow ledger reproduces the to_upload CSVs (the Phase 1 cutover gate,
runnable on demand after a write-through run).

For each artifact, render it from the ledger and compare to the correct CSV:
  - products:     per source, byte-identical to 3_products_<source>.csv
  - combinations: byte-identical to 2_valid_combinations.csv (sorted both sides)
Variants are NOT verified here: the ledger is seeded from variants_export, while
1_variants_full is the produced output (export union new union mirror), so they
differ until a catalog-level variation populate lands (a later step).

    python -m stone_pipeline.ledger.verify

Exits non-zero on any mismatch, so it can gate a build. No em dashes (design
principle 2).
"""

from __future__ import annotations

import csv
import filecmp
import sys
import tempfile
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core import logfmt
from stone_pipeline.ledger import writethrough
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.render import render_combinations, render_products
from stone_pipeline.stages import tree_build

log = logfmt.get_logger("ledger.verify")


def _sources_with_products(to_upload: Path) -> list[str]:
    return sorted(p.name[len("3_products_"):-len(".csv")]
                  for p in to_upload.glob("3_products_*.csv")
                  if p.name != "3_products_all.csv")


def verify_products(ledger: Ledger, to_upload: Path) -> list[str]:
    """Per source, render its products from the ledger and assert byte-identity with
    3_products_<source>.csv. Returns a list of mismatch messages (empty == all OK)."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for source in _sources_with_products(to_upload):
            correct = to_upload / f"3_products_{source}.csv"
            cfg = load_source(source)
            out = Path(tmp) / f"3_products_{source}.csv"
            render_products(ledger, cfg, out, source=cfg.source_code)
            if not filecmp.cmp(str(out), str(correct), shallow=False):
                problems.append(f"products mismatch for source '{source}' vs {correct.name}")
    return problems


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
