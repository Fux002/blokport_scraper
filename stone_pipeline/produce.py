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
    return build.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
