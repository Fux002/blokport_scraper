"""One-command build of a full upload phase, in dependency order, self-verifying.

    python -m stone_pipeline.build                                       # every enabled source -> catalog -> verify
    SCRAPER_RUN_VERIFY=1 python -m stone_pipeline.build                  # only re-run the consistency gate
    SCRAPER_RUN_SOURCES=zucchi python -m stone_pipeline.build            # ONE source -> catalog -> verify
    SCRAPER_RUN_STAGE=scrape SCRAPER_RUN_SOURCES=zucchi python -m stone_pipeline.build   # that source's products+stock ONLY
    SCRAPER_RUN_STAGE=catalog python -m stone_pipeline.build             # re-consolidate the SHARED catalog + verify
    SCRAPER_RUN_STAGE=republish python -m stone_pipeline.build           # re-run pipeline + catalog from the LAST scrape
    SCRAPER_RUN_STAGE=inventory python -m stone_pipeline.build           # cheap stock-only refresh, all sources
    SCRAPER_RUN_STAGE=inventory SCRAPER_RUN_SOURCES=zucchi python -m stone_pipeline.build   # one source's stock only

The two axes are independent: SCRAPER_RUN_SOURCES picks WHICH scrapers (default: all enabled), SCRAPER_RUN_STAGE
picks HOW FAR (scrape / catalog / inventory / all, the default). A per-source scrape is safe on live data --
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

from stone_pipeline import catalog as catalog_mod
from stone_pipeline import inventory as inventory_mod
from stone_pipeline import run as run_mod
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("build")

# `republish` re-runs the pipeline + catalog against whatever data/ already holds (the last scrape). At
# the BUILD level it is identical to `all` (build never fetches suppliers -- produce does); the two only
# diverge in produce.py, where `republish` skips the live scrape. It exists so an operator decision (e.g.
# a backbone-leaf approval) can release the products it just made valid without a costly supplier re-fetch.
STAGES = ("scrape", "catalog", "republish", "inventory", "all")
_FULL_STAGES = ("all", "republish")   # run BOTH the per-source pipeline and the shared catalog


def verify_gate() -> int:
    """Re-run ONLY the consistency gate on the existing files (SCRAPER_RUN_VERIFY). Ignores stage/sources."""
    errors, warnings = catalog_mod.verify_consistency()
    for w in warnings:
        print(f"  ! {w}")
        log.warning("consistency warning", extra={"extra_fields": {"warning": w}})
    for e in errors:
        print(f"  ✗ {e}")
        log.error("consistency error", extra={"extra_fields": {"error": e}})  # visible in CloudWatch
    print("consistency gate: PASS" if not errors else "consistency gate: FAIL")
    return 1 if errors else 0


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
        # catalog raises SystemExit with a REASON string (the consistency errors, or "no source runs
        # found ..."). Surface it instead of collapsing to a bare rc:1 -- else a failed catalog is
        # undiagnosable (a fresh task with no scrape on disk looks identical to a real gate failure).
        if not isinstance(exc.code, int) and exc.code:
            log.error("catalog stage aborted", extra={"extra_fields": {"reason": str(exc.code)}})
        return (exc.code if isinstance(exc.code, int) else 1) or 1


def _inventory(sources: list[str] | None) -> int:
    """Stage 'inventory': the lightweight stock clock. Same scrape -> match path but emits ONLY the
    stock delta into the ledger -- no product import, images, or catalog. All enabled sources, or a
    subset; source-scoped like the rest, so refreshing one supplier's stock never touches another's.
    (A full scrape already refreshes stock as part of its emit; this is the cheap stock-only path.)"""
    return inventory_mod.main(sources or [])


def run(sources: list[str] | None, stage: str) -> int:
    """Build the given stage over the given sources (None -> every enabled source). The param-driven core:
    the __main__ entry and produce call it with explicit values -- no argv. Validates the stage at the
    boundary (unknown -> rc 2, naming the allowed set), as the CLI did."""
    if stage not in STAGES:
        print(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
        log.error("build: unknown stage", extra={"extra_fields": {"stage": stage}})
        return 2

    # inventory is a standalone stage: the cheap stock-only refresh, no catalog/gate.
    if stage == "inventory":
        rc = _inventory(sources)
        scope = ", ".join(sources) if sources else "all enabled sources"
        if rc != 0:
            log.error("inventory refresh failed", extra={"extra_fields": {"rc": rc, "sources": sources}})
        else:
            print(f"\ninventory refresh complete ({scope}) -- stock delta written to the ledger")
        return rc

    # The dev seed (variants_export_base.csv) is maintained at the END of catalog.run as a copy of the
    # freshly-built 1_variants_full -- base IS the full list, one source of truth, no second path here.

    # 1. scrape -> per-source products + canonical (matches against the current export). Reads whatever is
    #    in data/ -- the live supplier fetch is produce's job, not this stage's.
    if stage in ("scrape",) + _FULL_STAGES:
        if (rc := _scrape(sources)) != 0:
            log.error("build aborted: scrape stage failed",
                      extra={"extra_fields": {"rc": rc, "sources": sources}})
            return rc
    # 2. consolidate variants + products + combinations, then run the consistency gate (catalog
    #    is SHARED across sources, so it always runs full even for a scoped scrape).
    if stage in ("catalog",) + _FULL_STAGES:
        if (rc := _catalog()) != 0:
            log.error("build aborted: catalog stage failed", extra={"extra_fields": {"rc": rc}})
            return rc

    scope = ", ".join(sources) if sources else "all enabled sources"
    print(f"\n{stage} complete ({scope}) -- see to_upload/ (and the ledger write-through)")
    return 0


def main() -> int:
    """The __main__ entry: read the run scope from the env ONCE (SETTINGS.run), then dispatch. No argv."""
    cfg = SETTINGS.run
    if cfg.verify:
        return verify_gate()
    return run(list(cfg.sources) or None, cfg.stage)


if __name__ == "__main__":
    raise SystemExit(main())
