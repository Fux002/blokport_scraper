"""Phase 1 equivalence: the ledger is a lossless representation of a to_upload CSV.

Round-trip proof for the variant layer: populate the variation table from the real
1_variants_full.csv, render it back through the same writer, and assert the bytes
are identical. This is the "ledger carries the same truth as the CSV" cutover
criterion (SYNC_LEDGER_DESIGN.md section 13). Until this passes, no CSV is retired.

Skipped when the dev artifact is absent (CI without data), so it never blocks a run.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.populate import populate_variations_full
from stone_pipeline.ledger.render import render_variants_full

VARIANTS_FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"


@pytest.mark.skipif(not VARIANTS_FULL.exists(), reason="no 1_variants_full.csv to check against")
def test_variants_full_roundtrip_byte_identical(tmp_path):
    ledger_path = tmp_path / "dev.ledger"
    out = tmp_path / "1_variants_full.csv"
    with Ledger.open(ledger_path, env="development") as ledger:
        loaded = populate_variations_full(ledger, VARIANTS_FULL)
        rendered = render_variants_full(ledger, out)

    # every CSV row became exactly one rendered row (in_full marks the produced set)
    assert rendered == loaded, f"row count drift: rendered {rendered} vs loaded {loaded}"

    original = VARIANTS_FULL.read_bytes()
    produced = out.read_bytes()
    assert produced == original, (
        f"variant round-trip is NOT byte-identical: {len(produced)} vs {len(original)} bytes"
    )
