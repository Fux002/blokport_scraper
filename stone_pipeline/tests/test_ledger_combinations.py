"""Combination equivalence: the full valid-combination set rendered from the
bootstrapped ledger is byte-identical to the correct 2_valid_combinations.csv.

Combinations are a sync-render (id-based, ~2M rows), so this is the end-to-end
comparison against the correct CSV: bootstrap the ledger from the real exports,
render through the same tree_build builder with the same external inputs the
pipeline uses, and assert the output file matches byte-for-byte.

Heavier than the other ledger tests (it builds the full 2M-row set); skipped when
the correct CSV or its inputs are absent, so it never blocks a run.
"""

from __future__ import annotations

import csv
import filecmp

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.bootstrap import seed_attributes, seed_variations
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.render import render_combinations
from stone_pipeline.stages import tree_build

CORRECT = SETTINGS.paths.to_upload_dir / "2_valid_combinations.csv"
EXPORT = SETTINGS.paths.variants_export_csv
ATTRS = SETTINGS.paths.attributes_csv


def _run_inputs():
    """The external inputs tree_build.run() feeds the builder, gathered the same way
    (everything except the export and attributes, which come from the ledger)."""
    products = SETTINGS.paths.to_upload_dir / "3_products_all.csv"
    uncovered = SETTINGS.paths.review_dir / "tree_uncovered_variations.csv"
    assigned = tree_build._load_assigned_types(uncovered, tree_build._load_attributes(ATTRS))
    delete_file = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    exclude_ids = set()
    if delete_file.exists():
        with delete_file.open(encoding="utf-8-sig") as h:
            exclude_ids = {(r.get("Id") or "").strip()
                           for r in csv.DictReader(h) if (r.get("Id") or "").strip()}
    return (products if products.exists() else None), assigned, exclude_ids


@pytest.mark.skipif(not (CORRECT.exists() and EXPORT.exists() and ATTRS.exists()),
                    reason="no correct 2_valid_combinations.csv (and inputs) to check against")
def test_combinations_render_matches_correct_csv(tmp_path):
    products, assigned, exclude_ids = _run_inputs()
    out = tmp_path / "2_valid_combinations.csv"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        seed_attributes(ledger)
        seed_variations(ledger)
        render_combinations(ledger, out, products_csv=products,
                            assigned_types=assigned, exclude_ids=exclude_ids)

    assert filecmp.cmp(str(out), str(CORRECT), shallow=False), (
        "ledger-rendered combinations are NOT byte-identical to the correct CSV"
    )
