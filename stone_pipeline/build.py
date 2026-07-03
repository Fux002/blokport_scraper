"""One-command build of a full upload phase, in dependency order, self-verifying.

    python -m stone_pipeline.build                          # every enabled source -> catalog -> verify
    python -m stone_pipeline.build --verify                 # only re-run the consistency gate
    python -m stone_pipeline.build --sources zucchi         # ONE source -> catalog -> verify
    python -m stone_pipeline.build --stage scrape --sources zucchi   # that source's products+stock ONLY
    python -m stone_pipeline.build --stage catalog          # re-consolidate the SHARED catalog + verify

The two axes are independent: `--sources` picks WHICH scrapers (default: all enabled), `--stage`
picks HOW FAR (scrape / catalog / all, the default). A per-source scrape is safe on live data --
products, stock and the delist are all SKU-prefix scoped to that source, and the shared catalog is
rebuilt only by the catalog stage, which always consolidates every source's latest run.

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

STAGES = ("scrape", "catalog", "all")


def _arg(argv: list[str], flag: str) -> str | None:
    """Value of `--flag val` or `--flag=val` in argv, else None (missing) / '' (flag, no value)."""
    for i, a in enumerate(argv):
        if a == flag:
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def parse_scope(argv: list[str]) -> tuple[list[str] | None, str]:
    """(sources, stage) from argv. sources None -> every enabled source; stage defaults to 'all'."""
    raw = _arg(argv, "--sources")
    sources = [s.strip() for s in raw.split(",") if s.strip()] if raw else None
    stage = (_arg(argv, "--stage") or "all").strip().lower()
    return sources, stage


def _scrape(sources: list[str] | None) -> int:
    """Stage 1: scrape products + stock into the ledger. `sources` None -> every enabled source (the
    aggregate `run all`, with its missing/blocked gate reporting); an explicit list -> just those,
    each via `run <source>` so that source's floor + delist guards still apply per source."""
    if not sources:
        return run_mod.main(["all"])
    rc = 0
    for s in sources:
        if (r := run_mod.main([s])) != 0:
            rc = r
    return rc


def _catalog() -> int:
    """Stage 2: consolidate the SHARED catalog (variants/combinations, ALL sources) + consistency
    gate. catalog raises SystemExit if the gate fails, so a bad upload set never returns success."""
    try:
        return catalog_mod.main([])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return code or 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if "--verify" in argv:
        errors, warnings = catalog_mod.verify_consistency()
        for w in warnings:
            print(f"  ! {w}")
            log.warning("consistency warning", extra={"extra_fields": {"warning": w}})
        for e in errors:
            print(f"  ✗ {e}")
            log.error("consistency error", extra={"extra_fields": {"error": e}})  # visible in CloudWatch
        print("consistency gate: PASS" if not errors else "consistency gate: FAIL")
        return 1 if errors else 0

    sources, stage = parse_scope(argv)
    if stage not in STAGES:
        print(f"unknown --stage {stage!r}; expected one of {', '.join(STAGES)}")
        log.error("build: unknown stage", extra={"extra_fields": {"stage": stage}})
        return 2

    # The dev seed (variants_export_base.csv) is maintained at the END of catalog.run as a copy of the
    # freshly-built 1_variants_full -- base IS the full list, one source of truth, no second path here.

    # 1. scrape -> per-source products + canonical (matches against the current export)
    if stage in ("scrape", "all"):
        if (rc := _scrape(sources)) != 0:
            log.error("build aborted: scrape stage failed",
                      extra={"extra_fields": {"rc": rc, "sources": sources}})
            return rc
    # 2. consolidate variants + products + combinations, then run the consistency gate (catalog
    #    is SHARED across sources, so it always runs full even for a scoped scrape).
    if stage in ("catalog", "all"):
        if (rc := _catalog()) != 0:
            log.error("build aborted: catalog/consistency gate failed", extra={"extra_fields": {"rc": rc}})
            return rc

    scope = ", ".join(sources) if sources else "all enabled sources"
    print(f"\n{stage} complete ({scope}) -- see to_upload/ (and the ledger write-through)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
