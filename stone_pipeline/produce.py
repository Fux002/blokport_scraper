"""Full from-scratch produce for the `/run` trigger (and ECS): fetch the Medusa export inputs from
S3, LIVE-scrape the sources, then build (pipeline + catalog / inventory) with ledger write-through.

This is what the config `/run` trigger runs, so a produce on a FRESH host -- a new ECS task with an
empty data/ -- populates the ledger from nothing. The batch run_pipeline.sh does the same fetch ->
scrape -> build; `python -m stone_pipeline.build` stays the laptop path (scrape a source separately,
then build against the local data). The split is deliberate: build assumes data/ is already there,
produce guarantees it. No em dashes (design principle 2).

    python -m stone_pipeline.produce                     # fetch -> scrape all -> build all
    python -m stone_pipeline.produce --sources zucchi     # ...that source only
    python -m stone_pipeline.produce --stage inventory    # fetch -> scrape -> stock-only refresh
    python -m stone_pipeline.produce --stage catalog      # NO scrape: just re-consolidate outputs

`--stage` / `--sources` pass straight through to build; only catalog-only skips the live scrape (it
re-consolidates existing outputs). Ledger write-through is the caller's job (the runner sets it).
"""

from __future__ import annotations

import sys

from stone_pipeline import build
from stone_pipeline.core import logfmt

log = logfmt.get_logger("produce")


def _fetch_inputs() -> None:
    """Pull the current Medusa export (variants_export + attributes) from S3 so the matcher resolves
    existing ids. Best-effort: a missing export degrades matching (new varieties still produce), so a
    failure -- no S3 on a laptop, an empty prefix on a fresh env -- logs and continues, never blocks
    a first produce."""
    try:
        from deploy import fetch_inputs
        fetch_inputs.main()
    except Exception:
        log.warning("fetch_inputs skipped; matching runs against whatever export is local",
                    exc_info=True)


def _live_scrape(sources: list[str] | None) -> int:
    """LIVE-fetch fresh supplier data into data/<source>/ -- what build's pipeline stage then reads.
    `sources` None -> every registered scraper (`run all`); an explicit list -> just those. A failed
    source surfaces as a non-zero rc so produce aborts before building against stale/absent data."""
    from scrapers import run as scrapers_run
    return scrapers_run.main(sources if sources else ["all"])


# The catalog consistency gate keys off variants_export.csv, so it FALSE-ALARMS on varieties minted
# THIS produce (no Medusa id yet). These two error classes are that expected two-pass checkpoint; any
# error OUTSIDE them keeps the gate fatal.
_NEW_VARIETY_MARKERS = ("NOT in the current export", "NO valid-combination row")


def _ledger_gate_state() -> tuple[int, int, int]:
    """(held, untyped, dangling) from the ledger -- the pull-model source of truth (NOT the stale CSV
    export), which is why pass-2 needs no refreshed Medusa export:
      held     produced variations with NO Medusa id yet -- awaiting the first pull, whose ack assigns
               medusa_id + flips them synced. EXPECTED, not an error.
      untyped  a SUBSET of held that still lacks a canonical type: the sync engine holds these until
               typed (never serves an untyped variation), so it is informational, NOT fatal.
      dangling products whose variation_key has no variation row: a real structural orphan -> fatal.
    Only `dangling` is a genuine fault; `held`/`untyped` are the expected two-pass state."""
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        def n(sql: str) -> int:
            return lg.execute(sql).fetchone()["n"]
        held = n("SELECT COUNT(*) n FROM variation WHERE in_full = 1 AND medusa_id IS NULL "
                 "AND state IN ('pending', 'dirty')")
        untyped = n("SELECT COUNT(*) n FROM variation WHERE in_full = 1 AND medusa_id IS NULL "
                    "AND state IN ('pending', 'dirty') AND (type IS NULL OR type = '')")
        dangling = n("SELECT COUNT(*) n FROM product WHERE variation_key NOT IN (SELECT key FROM variation)")
    return held, untyped, dangling


def _reconcile_gate(rc: int) -> int:
    """Pull-model reconciliation of the catalog consistency gate. The gate is a CSV-upload-era guard: it
    keys off variants_export.csv, so it fails on varieties minted this produce (no Medusa id yet). But
    the sync engine already gates each product on its variation being synced (_ELIGIBLE_PRODUCT), and
    the pull ack IS the round-trip that mints the id -- so this is the expected pass-1 checkpoint, not a
    real fault. When the gate's ONLY failures are that class, new varieties explain them (held > 0), and
    nothing is structurally orphaned (dangling == 0), exit 0 reporting the held count; else stay fatal."""
    from stone_pipeline import catalog as catalog_mod
    errors, _ = catalog_mod.verify_consistency()
    if not errors:
        return rc                                   # gate isn't why build failed -- keep the failure
    if any(not any(m in e for m in _NEW_VARIETY_MARKERS) for e in errors):
        return rc                                   # an error outside the new-variety class -> fatal
    held, untyped, dangling = _ledger_gate_state()
    if dangling or not held:
        return rc                                   # a structural orphan, or nothing new explains it -> fatal
    msg = f"held: {held} new variations awaiting pull round-trip"
    if untyped:
        msg += f" ({untyped} still need a type -- the sync holds them until then)"
    log.warning("catalog gate held (non-fatal): the shortfall is new varieties awaiting the pull",
                extra={"extra_fields": {"held": held, "untyped": untyped}})
    print(msg + " (exit 0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # --verify is a pure re-check of what is already built: no fetch, no scrape -- delegate straight.
    if "--verify" in argv:
        return build.main(argv)

    sources, stage = build.parse_scope(argv)
    if stage not in build.STAGES:
        return build.main(argv)   # let build report the unknown stage consistently

    _fetch_inputs()
    # catalog-only re-consolidates existing outputs/; every other stage reads fresh scrape data.
    if stage != "catalog":
        if (rc := _live_scrape(sources)) != 0:
            log.error("produce aborted: live scrape failed",
                      extra={"extra_fields": {"rc": rc, "sources": sources or "all"}})
            return rc
    rc = build.main(argv)
    # Pull model (write-through on): a catalog-gate failure that is purely new-this-run varieties is the
    # expected two-pass checkpoint, not an error -- reconcile against the ledger so pass-1 exits 0.
    if rc != 0 and stage in ("catalog", "all"):
        from stone_pipeline.ledger import writethrough
        if writethrough.enabled():
            return _reconcile_gate(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
