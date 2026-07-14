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


def verify(base_path: Path = BASE) -> dict:
    """Rebuild 1_variants_full from the committed base and assert it projects back to the SAME base -- the
    fixed-point property that makes repeated cold starts reproducible. Non-destructive: only the generated
    1_variants_full is (re)written; the committed base is read, never modified.

    Run from a clean tree with no pending 1_variants_update delta (the cold-start seed state); a stale
    delta legitimately makes base != full, which this correctly reports as not-a-fixed-point."""
    from stone_pipeline.stages import emit_catalog
    emit_catalog.build()                       # -> to_upload/1_variants_full.csv (a generated artifact)
    base, full = _rows(base_path), _rows(FULL)
    first = next((i for i, (b, f) in enumerate(zip(base, full)) if b != f), None)
    fixed = base == full
    stats = {"fixed_point": fixed, "base_rows": len(base), "full_rows": len(full), "first_diff_row": first}
    (log.info if fixed else log.error)(
        "seed is a fixed point (base == rebuilt full)" if fixed
        else "seed is NOT a fixed point: base would drift on the next build",
        extra={"extra_fields": stats})
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
        print(f"seed fixed point: {s['fixed_point']} (base {s['base_rows']} rows"
              + (f", first diff at row {s['first_diff_row']}" if not s['fixed_point'] else "") + ")")
        return 0 if s["fixed_point"] else 1
    if cmd == "reset":
        s = reset()
        print(f"reset {s['reset']} to committed seed ({s['rows']} rows)")
        return 0
    print(f"unknown command {cmd!r}; use 'verify' or 'reset'")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
