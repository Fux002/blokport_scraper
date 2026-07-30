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


def _duplicate_varieties(rows: list[dict]) -> list[tuple[str, str, str]]:
    """Every (branch, type, Name) that resolves to MORE THAN ONE Key. A variety is identified by its
    stone type + display name; two Keys for the same (type, Name) are the same stone twice -- the class
    that ships duplicate products. This is invisible to the fixed-point check (distinct Keys make base a
    stable fixed point WITH the dup present), so it is asserted separately.

    The type is the FULL type slug (type_slug_from_key), NOT the Key's first token: a multi-word type
    (dolomite marble, semi precious stone, lava stone) has several tokens, so keying on parts[1] alone
    conflates 'dolomite marble' with 'dolomite' and false-flags two genuinely different-typed varieties
    of the same name as a duplicate. Identity matches the emit's union-fill (proj.norm type + name)."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import type_slug_from_key
    seen: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = r.get("Key") or ""
        ident = (key.split("_", 1)[0], proj.norm(type_slug_from_key(key)), proj.norm(r.get("Name") or ""))
        seen[ident] = seen.get(ident, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def _malformed_type_keys(rows: list[dict]) -> list[str]:
    """Keys whose type slug is NOT a real Medusa stone type -- a type-less / mis-keyed variety
    (e.g. `slab_alpine_luxe`, minted before the type-less-mint guard existed: gen_key drops an empty
    type, so the branch is followed straight by the name). Like a duplicate, this is invisible to the
    fixed-point check (a distinct malformed Key is a stable fixed point), so it is asserted separately --
    the base must carry only properly-typed Keys, or a cold start ships type-less varieties to Medusa."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_attributes, type_slug_from_key
    valid = {proj.norm(t) for t in load_attributes().canonical_names("type")}
    return sorted(r["Key"] for r in rows
                  if (ts := proj.norm(type_slug_from_key(r.get("Key") or ""))) and ts not in valid)


def _non_uniform_categories(rows: list[dict]) -> list[str]:
    """Categories whose variety set (type, name) is not the FULL UNION across all categories. A variety
    belongs to the stone, not a format, so every category must carry every variety any category has -- a
    variety present in one category but not another (a slab with no block, a block-only variety missing
    from slab) means a cold start would ship an inconsistent catalog. Compared against the UNION, not a
    master category, so a block-only variety flags slab AND tile (no category is privileged); a future
    category is covered automatically -- any category present in the base must equal the union."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import type_slug_from_key
    by_cat: dict[str, set] = {}
    for r in rows:
        br = (r.get("Key") or "").split("_")[0]
        if br:
            by_cat.setdefault(br, set()).add(
                (proj.norm(type_slug_from_key(r["Key"])), proj.norm(r.get("Name", ""))))
    union: set = set().union(*by_cat.values()) if by_cat else set()
    return sorted(c for c, s in by_cat.items() if s != union)


def _mistyped_variants(rows: list[dict]) -> list[str]:
    """Keys whose NAME carries a stone-type word that contradicts the Key's type (loaders.
    is_mistyped_variant) -- e.g. 'Agata White' (an agate) keyed as semi_precious_stone. The pipeline
    treats these as errors (excluded from matching, listed for deletion) and the emit drops them, so the
    committed base must carry none. Left in, a mistyped variety survives in whichever category has not yet
    been cleaned in Medusa and vanishes from the rest, breaking uniformity."""
    from stone_pipeline.reference.loaders import is_mistyped_variant
    return sorted(r["Key"] for r in rows if is_mistyped_variant(r.get("Key") or "", r.get("Name") or ""))


def verify(base_path: Path = BASE) -> dict:
    """Rebuild 1_variants_full from the committed base and assert (1) it projects back to the SAME base --
    the fixed-point property that makes repeated cold starts reproducible -- and (2) no (branch,type,Name)
    variety is present under more than one Key. Non-destructive: only the generated 1_variants_full is
    (re)written; the committed base is read, never modified.

    Run from a clean tree with no pending 1_variants_update delta (the cold-start seed state); a stale
    delta legitimately makes base != full, which this correctly reports as not-a-fixed-point."""
    from stone_pipeline.stages import emit_catalog
    emit_catalog.build()                       # -> to_upload/1_variants_full.csv (a generated artifact)
    base, full = _rows(base_path), _rows(FULL)
    first = next((i for i, (b, f) in enumerate(zip(base, full)) if b != f), None)
    fixed = base == full
    dups = _duplicate_varieties(base)          # a duplicate variety is invisible to the fixed-point check
    malformed = _malformed_type_keys(base)     # type-less / mis-keyed varieties, also fixed-point-invisible
    nonuniform = _non_uniform_categories(base)  # every category must carry the full variety union
    mistyped = _mistyped_variants(base)         # name's type word contradicts the Key type; emit drops these
    stats = {"fixed_point": fixed, "base_rows": len(base), "full_rows": len(full),
             "first_diff_row": first, "duplicate_varieties": len(dups),
             "malformed_type_keys": len(malformed), "non_uniform_categories": nonuniform,
             "mistyped_variants": len(mistyped),
             "clean": fixed and not dups and not malformed and not nonuniform and not mistyped}
    (log.info if stats["clean"] else log.error)(
        "seed is a fixed point: no duplicate/type-less/mistyped varieties, categories uniform" if stats["clean"]
        else "seed FAILED: not a fixed point and/or duplicate/type-less/mistyped/non-uniform categories",
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
