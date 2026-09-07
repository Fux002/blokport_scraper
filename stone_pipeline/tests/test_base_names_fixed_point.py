"""Every committed base variety Name is the pipeline's own identity name (clean_variety fixed point).

A base Name that clean_variety would rewrite can never bind a scraped product exactly: identity is
(type, name) and the scraper reduces every product name through clean_variety before matching, so
'Taj Mahal Quartzite' in the base vs 'Taj Mahal' from the cleaner is a permanent hold or a duplicate.
The lot-code allowlist covers names the trailing <=2-char rule mangles ('White Phu My' is a place, not
a code): they stay as-is until that rule is fixed, and the test fails if one silently disappears.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from stone_pipeline.adapters.tokens import clean_variety
from stone_pipeline.reference.loaders import type_slug_from_key

_ROOT = Path(__file__).resolve().parents[2]
_BASES = sorted((_ROOT / "from_medusa").glob("*/variants_export_base.csv"))

# Names the cleaner's trailing two-letter-code rule would truncate; deliberately kept verbatim.
# 'Silver Travertine Marble' keeps its trailing type on purpose: stripped, 'Silver Travertine' reads as a
# travertine under a marble Key and the mistyped gate would drop it.
_LOT_CODE_ALLOWLIST = frozenset({
    "Absolute Black Ds", "Baltic Brown Bb", "White Super Es", "Milano Venato Pi", "Green Shakh do",
    "Moca Creme Az", "Sao Rafael Gm", "Bianco Corte Ga", "White Phu My", "Yellow Nghe An",
    "Silver Travertine Marble",
})


@pytest.mark.parametrize("base", _BASES, ids=lambda p: p.parent.name)
def test_committed_base_names_are_clean_variety_fixed_points(base: Path):
    rows = list(csv.DictReader(base.open(encoding="utf-8-sig")))
    assert rows, base
    drifted, seen_allowlisted = [], set()
    for r in rows:
        name, typ = r["Name"], type_slug_from_key(r["Key"]).replace("_", " ")
        if name in _LOT_CODE_ALLOWLIST:
            seen_allowlisted.add(name)
            continue
        cleaned = clean_variety(name, typ)
        if cleaned.lower() != name.lower():
            drifted.append((r["Key"], name, cleaned))
    assert not drifted, f"{len(drifted)} base names are not clean_variety fixed points, e.g. {drifted[:5]}"
    assert seen_allowlisted == _LOT_CODE_ALLOWLIST, (
        f"allowlist drift: missing {_LOT_CODE_ALLOWLIST - seen_allowlisted}; drop them from the allowlist")
