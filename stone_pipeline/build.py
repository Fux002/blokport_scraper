"""One-command build of a full upload phase, in dependency order, self-verifying.

    python -m stone_pipeline.build            # scrape -> products + variants + combinations + verify
    python -m stone_pipeline.build --verify   # only re-run the consistency gate on existing files

This replaces hand-sequencing `run all` -> `catalog` -> `tree`. That manual ordering had no guard:
forgetting `tree` (or running it before a fresh export) silently shipped STALE combinations. Here the
steps run in order, fail fast, and end on a deterministic consistency gate, so a partial or
out-of-order run can never produce an inconsistent upload set.

The full catalog cycle still has a human step in the middle -- upload the variants into Medusa and
refresh from_medusa/<env>/variants_export.csv -- so the production loop is:

    python -m stone_pipeline.build      # build upload files
    # ... upload 1_variants_* into Medusa, re-export to variants_export.csv ...
    python -m stone_pipeline.build      # rebuild: new ids resolve, combinations + products refresh
"""

from __future__ import annotations

import sys

from stone_pipeline import catalog as catalog_mod
from stone_pipeline import run as run_mod
from stone_pipeline.core import logfmt

log = logfmt.get_logger("build")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if "--verify" in argv:
        errors, warnings = catalog_mod.verify_consistency()
        for w in warnings:
            print(f"  ! {w}")
        for e in errors:
            print(f"  ✗ {e}")
        print("consistency gate: PASS" if not errors else "consistency gate: FAIL")
        return 1 if errors else 0

    # 1. scrape -> per-source products + canonical (matches against the current export)
    rc = run_mod.main(["all"])
    if rc != 0:
        log.error("build aborted: `run all` failed", extra={"extra_fields": {"rc": rc}})
        return rc
    # 2. consolidate variants + products + combinations, then run the consistency gate. catalog
    #    raises SystemExit if the gate fails, so a bad upload set never returns success.
    try:
        rc = catalog_mod.main([])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        log.error("build aborted: catalog/consistency gate failed", extra={"extra_fields": {"detail": str(exc.code)}})
        return code or 1
    if rc != 0:
        log.error("build aborted: catalog failed", extra={"extra_fields": {"rc": rc}})
        return rc
    print("\nbuild complete + consistency-verified -- upload files are ready in to_upload/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
