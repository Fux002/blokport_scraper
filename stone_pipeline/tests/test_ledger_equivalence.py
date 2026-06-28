"""Phase 1 equivalence: the ledger is a lossless representation of a to_upload CSV.

Round-trip proof for the variant layer: load the real 1_variants_full.csv into a
fresh ledger, render it back through the same writer, and assert the bytes are
identical. This is the "ledger carries the same truth as the CSV" cutover criterion
(SYNC_LEDGER_DESIGN.md section 13). Until this passes, no CSV is retired.

The test is skipped when the dev artifact is absent (CI without data), so it never
blocks a run; it asserts equivalence only when there is real data to check.

The reverse-loader here is TEST scaffolding, not the production populate (which
fills the full canonical fields from canonical rows). It fills only the columns the
variants CSV carries.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import Ledger, now_iso, payload_hash
from stone_pipeline.ledger.render import render_variants_full
from stone_pipeline.stages.emit_catalog import _COLS as COLS

VARIANTS_FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"


def _branch_of(key: str) -> str:
    head = key.split("_", 1)[0]
    return head if head in ("slab", "block", "tile") else ""


def _load_variants_full_csv(ledger: Ledger, path: Path) -> int:
    """Reverse-load a 1_variants_full.csv into `variation`, in file order. Fills only
    the CSV columns; branch is parsed from the Key, type is left blank (the variants
    file does not carry the canonical stone type)."""
    now = now_iso()
    n = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            key = (r.get("Key") or "").strip()
            if not key:
                continue
            name = r.get(COLS[1]) or ""
            image_url = r.get(COLS[2]) or ""
            aliases_cell = r.get(COLS[3]) or ""
            aliases = [a for a in aliases_cell.split("|") if a]
            volume = r.get(COLS[4]) or ""
            branch = _branch_of(key)
            ledger.upsert("variation", {
                "key": key,
                "branch": branch,
                "type": "",
                "name": name,
                "aliases": json.dumps(aliases),
                "image_url": image_url,
                "image_sha256": None,
                "image_model": None,
                "volume": volume,
                "medusa_id": None,
                "payload_hash": payload_hash([branch, "", name, sorted(aliases), image_url, volume]),
                "state": "synced",
                "first_seen": now,
                "last_synced": now,
                "created_at": now,
                "updated_at": now,
            }, pk=("key",))
            n += 1
    return n


@pytest.mark.skipif(not VARIANTS_FULL.exists(), reason="no 1_variants_full.csv to check against")
def test_variants_full_roundtrip_byte_identical(tmp_path):
    ledger_path = tmp_path / "dev.ledger"
    out = tmp_path / "1_variants_full.csv"
    with Ledger.open(ledger_path, env="development") as ledger:
        loaded = _load_variants_full_csv(ledger, VARIANTS_FULL)
        rendered = render_variants_full(ledger, out)

    # every CSV row became exactly one ledger row (no silent key collisions)
    assert rendered == loaded, f"row count drift: rendered {rendered} vs loaded {loaded}"

    original = VARIANTS_FULL.read_bytes()
    produced = out.read_bytes()
    assert produced == original, (
        f"variant round-trip is NOT byte-identical: {len(produced)} vs {len(original)} bytes"
    )
