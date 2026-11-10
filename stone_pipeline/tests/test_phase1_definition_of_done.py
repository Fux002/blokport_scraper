"""SC-4's definition of done (PHASE-1-SPEC section 8; plan SC-3-SC-4-PLAN.md section 2a): no code reads a variety's
category from its Key any more. None of these appears in the code base outside the store migration (the
`stone_pipeline/phase1` package and its `test_phase1_*` tests, which read the OLD Keys on purpose) and the test
fixtures:
- the old helpers `category_for_key`, `mirror_of` and `_branch_of`;
- the Key prefix split `split("_", 1)[0]` and the type slice `split("_")[1:-1]`;
- a prefixed Key literal: a quoted Key-shaped string starting `slab_`, `block_` or `tile_`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("stone_pipeline", "scrapers", "deploy", "image_pipeline")
FORBIDDEN = {
    "category_for_key": re.compile(r"\bcategory_for_key\b"),
    "mirror_of": re.compile(r"\bmirror_of\b"),
    "_branch_of": re.compile(r"\b_branch_of\b"),
    "Key prefix split": re.compile(r"""split\(\s*["']_["']\s*,\s*1\s*\)\s*\[\s*0\s*\]"""),
    "Key type slice": re.compile(r"""split\(\s*["']_["']\s*\)\s*\[\s*1\s*:\s*-1\s*\]"""),
    # a Key-shaped literal: the prefix, word segments, and a uuid start or a numeric suffix ('slab_marble_x_1',
    # 'tile_onyx_white_3f2a9c1b-...'); a field name like 'slab_count' is not one
    "prefixed Key literal": re.compile(r"""["'](?:slab|block|tile)_(?:[a-z0-9]+_)+(?:[0-9a-f]{8}-|\d+["'])"""),
}


def _exempt(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return (rel.startswith("stone_pipeline/phase1/") or rel.startswith("stone_pipeline/tests/fixtures/")
            or (rel.startswith("stone_pipeline/tests/test_phase1_") and rel.endswith(".py")))


def test_no_code_reads_a_category_from_a_variety_key():
    found = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if _exempt(path) or path.resolve() == Path(__file__).resolve():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for what, pattern in FORBIDDEN.items():
                    if pattern.search(line):
                        found.append(f"{path.relative_to(ROOT)}:{n}: {what}")
    assert found == [], "\n".join(found[:200])
