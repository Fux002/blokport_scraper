"""Seed integrity: verify the committed cold-start seed is a FIXED POINT, and reset the self-mutating
seed file back to its committed state.

The catalog build overwrites variants_export_base.csv every run (base := 1_variants_full, via
sync_variants_base). For repeated cold starts to reproduce byte-identically, the committed base must be
a fixed point of the build: rebuilding from it must project back to the SAME base. `verify` asserts that
(non-destructively); `reset` restores the committed base so an operator can return to seed before a cold
start, undoing any in-place mutation a prior run left behind.

    python -m stone_pipeline.reference.seed verify
    python -m stone_pipeline.reference.seed reset
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.reference.sync_variants_base import COLS

log = logfmt.get_logger("seed")

# The self-mutating committed seed: rebuilt (base := full) at the end of every catalog.run.
BASE = SETTINGS.paths.from_medusa_dir / "variants_export_base.csv"
FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"


def _rows(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig") as h:
        return [{c: (r.get(c) or "") for c in COLS} for r in csv.DictReader(h)]


def _duplicate_varieties(rows: list[dict]) -> list[tuple[str, str]]:
    """Every (type, Name) that resolves to MORE THAN ONE Key. A variety is identified by its stone type + display
    name (phase 1: one record per (type, name)); two Keys for the same (type, Name) are the same stone twice -- the
    class that ships duplicate products. This is invisible to the fixed-point check (distinct Keys make base a
    stable fixed point WITH the dup present), so it is asserted separately. The type is the Key's FULL type slug
    (the one parser), so 'dolomite marble' and 'dolomite' never conflate."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import type_slug_from_key
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        ident = (proj.norm(type_slug_from_key(r.get("Key") or "")), proj.norm(r.get("Name") or ""))
        seen[ident] = seen.get(ident, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def _malformed_type_keys(rows: list[dict]) -> list[str]:
    """Keys whose type slug is NOT a real Medusa stone type -- a type-less / mis-keyed variety
    (e.g. a Key whose type slug is `alpine`, minted before the type-less-mint guard existed). Like a duplicate, this is invisible to the
    fixed-point check (a distinct malformed Key is a stable fixed point), so it is asserted separately --
    the base must carry only properly-typed Keys, or a cold start ships type-less varieties to Medusa."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_attributes, type_slug_from_key
    valid = {proj.norm(t) for t in load_attributes().canonical_names("type")}
    return sorted(r["Key"] for r in rows
                  if (ts := proj.norm(type_slug_from_key(r.get("Key") or ""))) and ts not in valid)


def _mistyped_variants(rows: list[dict]) -> list[str]:
    """Keys whose NAME carries a stone-type word that contradicts the Key's type (loaders.
    is_mistyped_variant) -- e.g. 'Agata White' (an agate) keyed as semi_precious_stone. The pipeline
    treats these as errors (excluded from matching, listed for deletion) and the emit drops them, so the
    committed base must carry none. Left in, a mistyped variety survives in whichever category has not yet
    been cleaned in Medusa and vanishes from the rest, breaking uniformity."""
    from stone_pipeline.reference.loaders import is_mistyped_variant
    return sorted(r["Key"] for r in rows if is_mistyped_variant(r.get("Key") or "", r.get("Name") or ""))


def verify(base_path: Path = BASE) -> dict:
    """Assert the committed seed is a FIXED POINT: rebuilding HERMETICALLY from the base projects its SEED
    IDENTITY back to the SAME base, and no (type, Name) variety is present under more than one Key.
    Non-destructive: reads the base, writes only a throwaway temp file.

    HERMETIC by construction -- the rebuild uses `emit_catalog.build(seed_only=True)`, which reads ONLY the
    committed base (no live Medusa export, no update delta, no config.db retired decisions, no product-backed
    lookup, no live S3). So the result depends solely on the committed seed, never on the operator's shell,
    S3 credentials, or local state, and every column is reproduced deterministically.
    This is what makes the check give the SAME answer everywhere (CI, a clean tree, or an S3-authenticated
    dev shell), instead of the old "seed FAILED" that only meant the ambient environment differed."""
    from stone_pipeline.stages import emit_catalog
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        full_path = Path(_td) / "1_variants_full.verify.csv"
        emit_catalog.build(seed_only=True, out_path=full_path)   # hermetic seed projection; never clobbers
        base, full = _rows(base_path), _rows(full_path)
    first = next((i for i, (b, f) in enumerate(zip(base, full)) if b != f), None)
    fixed = base == full
    dups = _duplicate_varieties(base)          # a duplicate variety is invisible to the fixed-point check
    malformed = _malformed_type_keys(base)     # type-less / mis-keyed varieties, also fixed-point-invisible
    mistyped = _mistyped_variants(base)         # name's type word contradicts the Key type; emit drops these
    stats = {"fixed_point": fixed, "base_rows": len(base), "full_rows": len(full),
             "first_diff_row": first, "duplicate_varieties": len(dups),
             "malformed_type_keys": len(malformed), "mistyped_variants": len(mistyped),
             "clean": fixed and not dups and not malformed and not mistyped}
    (log.info if stats["clean"] else log.error)(
        "seed is a fixed point: no duplicate/type-less/mistyped varieties" if stats["clean"]
        else "seed FAILED: not a fixed point and/or duplicate/type-less/mistyped varieties",
        extra={"extra_fields": {**stats, "dups": dups[:20], "malformed": malformed[:20], "mistyped": mistyped[:20]}})
    return stats


def reset(base_path: Path = BASE) -> dict:
    """Restore the self-mutating base seed to its committed git state, so a cold start begins from the
    canonical committed seed. Fails loud if the file is untracked or git is unavailable -- it must never
    silently leave a mutated seed in place."""
    path = Path(base_path)
    proc = subprocess.run(
        ["git", "checkout", "HEAD", "--", str(path)],
        cwd=str(SETTINGS.paths.workspace_root), capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"seed reset FAILED for {path.name}: {proc.stderr.strip()} "
                         "(is it committed, and is this a git checkout?)")
    log.info("seed reset to committed state", extra={"extra_fields": {"file": str(path)}})
    return {"reset": str(path), "rows": len(_rows(path))}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "verify"
    if cmd == "verify":
        s = verify()
        print(f"seed clean: {s['clean']} (base {s['base_rows']} rows, fixed_point={s['fixed_point']}, "
              f"duplicate_varieties={s['duplicate_varieties']}"
              + (f", first diff at row {s['first_diff_row']}" if not s['fixed_point'] else "") + ")")
        return 0 if s["clean"] else 1
    if cmd == "reset":
        s = reset()
        print(f"reset {s['reset']} to committed seed ({s['rows']} rows)")
        return 0
    print(f"unknown command {cmd!r}; use 'verify' or 'reset'")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
