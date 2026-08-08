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
    python -m stone_pipeline.produce --stage republish    # NO scrape: re-run pipeline + catalog from cache

`--stage` / `--sources` pass straight through to build; `catalog` and `republish` skip the live scrape
and reuse the last scrape (catalog only consolidates; republish re-runs the whole pipeline + catalog, so
an operator decision like a backbone-leaf approval releases the products it just made valid without a
supplier re-fetch). Ledger write-through is the caller's job (the runner sets it).
"""

from __future__ import annotations

import sys

from stone_pipeline import build
from stone_pipeline.core import logfmt

log = logfmt.get_logger("produce")


# Stages that skip the live supplier fetch and reuse the last scrape in data/.
_NO_SCRAPE_STAGES = ("catalog", "republish")
# Stages that produced a catalog (so the cold-start + consistency reconcile guard applies).
_CATALOG_STAGES = ("catalog", "all", "republish")


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
    # Realign the category registry with the export NOW on disk. The pcats were read once at import
    # (bootstrap, BEFORE this fetch); a produce that just pulled a changed attributes.csv -- a new Medusa
    # pcat id or a corrected category label -- would otherwise keep gating emit + new-variety fan-out on
    # the stale import snapshot (symptom: every row category_invalid). Runs even when the fetch was
    # skipped (re-reads the same local file the import used), so it can only ever correct, never regress.
    from stone_pipeline.config import settings
    settings.refresh_category_pcats()


def _acquires_scraper(source: str) -> bool:
    """True when this source is fetched by a scraper (data/<source>/...). A load_frame/file_drop source
    acquires its own frame in the pipeline (adapter.load_frame / a dropped file), so it must NOT be sent
    to the scraper runner, which would reject an unknown scraper name. Unknown source -> treated as a
    scraper so the runner reports it, not silently dropped."""
    from stone_pipeline import adapters as adapter_registry
    from stone_pipeline.adapters.base import ACQ_SCRAPER
    adapter = adapter_registry.REGISTRY.get(source)
    return adapter is None or adapter.acquires_via() == ACQ_SCRAPER


def _live_scrape(sources: list[str] | None) -> int:
    """LIVE-fetch fresh supplier data into data/<source>/ -- what build's pipeline stage then reads.
    `sources` None -> every registered scraper (`run all`); an explicit list -> just the SCRAPER sources
    in it (load_frame/file_drop sources need no supplier fetch; the pipeline acquires their frames). A
    failed scraper surfaces as a non-zero rc so produce aborts before building against stale/absent data."""
    from scrapers import run as scrapers_run
    if not sources:
        return scrapers_run.main(["all"])
    scraper_sources = [s for s in sources if _acquires_scraper(s)]
    if not scraper_sources:
        return 0    # all load_frame/file_drop: nothing to live-scrape; build acquires their frames
    return scrapers_run.main(scraper_sources)


# The catalog consistency gate keys off variants_export.csv, so it FALSE-ALARMS on the two-pass pull
# checkpoint: (1) varieties minted THIS produce have no Medusa id yet ("NOT in the current export" /
# "NO valid-combination row"), and (2) a PRISTINE cold start has an EMPTY export, so the gate cannot
# verify anything against it ("cannot verify") -- the same condition tree_build already treats as a soft
# cold start (it writes empty combinations so the variants flow to Medusa). All three are rescued ONLY
# when the ledger confirms new variations explain them (held > 0); any error OUTSIDE this class, or
# held == 0 (e.g. a warm run whose export genuinely broke, with nothing new), keeps the gate fatal.
_COLD_START_MARKERS = ("NOT in the current export", "NO valid-combination row", "cannot verify")


def _ledger_gate_state() -> tuple[int, int]:
    """(held, untyped) from the ledger -- the pull-model source of truth (NOT the stale CSV export),
    which is why pass-2 needs no refreshed Medusa export. The schema queries live in the ledger layer
    (sync.gate_state); produce owns only the held-vs-fatal DECISION made from them."""
    from stone_pipeline.ledger import sync, writethrough
    from stone_pipeline.ledger.db import Ledger
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        return sync.gate_state(lg)


def _cold_start_stall() -> str | None:
    """A reason string when this produce typed NOTHING it can serve, else None. Typing is a hard serve
    gate, so an empty type vocabulary (attributes export absent/unseeded) or 0 typed of a non-empty
    produced set means the WHOLE catalogue is held untyped forever -- a broken cold start, not the
    expected pass-1 hold. produced == 0 (a no-op re-run) is not a stall."""
    from stone_pipeline.ledger import sync, writethrough
    from stone_pipeline.ledger.db import Ledger
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        vocab, produced, typed = sync.typing_health(lg)
    if produced == 0:
        return None
    if vocab == 0:
        return (f"type attribute vocabulary is EMPTY (the Medusa attributes export is absent/unseeded); "
                f"0 of {produced} variations could be typed, so NOTHING will serve")
    if typed == 0:
        return (f"0 of {produced} produced variations are typed; the catalogue would serve nothing "
                f"(check the attribute vocabulary and the Key type slugs)")
    return None


def _reconcile_gate(rc: int) -> int:
    """Pull-model reconciliation of the catalog consistency gate. The gate is a CSV-upload-era guard: it
    keys off variants_export.csv, so it fails on varieties minted this produce (no Medusa id yet). But
    the sync engine already gates each product on its variation being synced (_ELIGIBLE_PRODUCT), and
    the pull ack IS the round-trip that mints the id -- so this is the expected pass-1 checkpoint, not a
    real fault. When the gate's ONLY failures are that class and new varieties explain them (held > 0),
    exit 0 reporting the held count; else stay fatal."""
    from stone_pipeline import catalog as catalog_mod
    errors, _ = catalog_mod.verify_consistency()
    if not errors:
        return rc                                   # gate isn't why build failed -- keep the failure
    if any(not any(m in e for m in _COLD_START_MARKERS) for e in errors):
        return rc                                   # an error outside the cold-start/new-variety class -> fatal
    held, untyped = _ledger_gate_state()
    if not held:
        return rc                                   # nothing new explains the failure -> stay fatal
    msg = f"held: {held} new variations awaiting pull round-trip"
    if untyped:
        msg += f" ({untyped} still need a type -- the sync holds them until then)"
    log.warning("catalog gate held (non-fatal): the shortfall is new varieties awaiting the pull",
                extra={"extra_fields": {"held": held, "untyped": untyped}})
    print(msg + " (exit 0)")
    return 0


def _finalize_control_plane(stage: str) -> None:
    """Fold this run's per-source diagnostics into config.db and run the admission reaction (auto-demote on
    drift), so EVERY produce path populates :4200's diagnostics + the auto-demote safety -- not only the
    config server's local watcher. Previously these ran only in runner._watch_local, so a CLI produce or an
    ECS-dispatched task never persisted diagnostics and never demoted a drifting auto source.

    Skips an INVENTORY run: it is a lightweight stock refresh that does NOT run the full validate path, so it
    must not advance the admission consistency streak (N clean VALIDATED runs -> eligible) nor overwrite the
    latest full-run diagnostic with an all-skipped one. (`catalog` is left running -- it does no per-source
    run, so evaluate_from_outputs just replays the already-recorded latest and is a no-op.)

    Best-effort + idempotent: both stores upsert by run_id/source, so this is a no-op replay when the local
    watcher also runs it (which is KEPT, because it fires even when a hung subprocess is SIGKILLed). It never
    changes produce's rc."""
    if stage == "inventory":
        return
    try:
        from stone_pipeline.config import admission, diagnostics
        diagnostics.persist_from_outputs()
        admission.evaluate_from_outputs()
    except Exception:
        log.exception("control-plane finalize (diagnostics/admission) failed; non-fatal")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # --verify is a pure re-check of what is already built: no fetch, no scrape -- delegate straight.
    if "--verify" in argv:
        return build.main(argv)

    sources, stage = build.parse_scope(argv)
    if stage not in build.STAGES:
        return build.main(argv)   # let build report the unknown stage consistently

    _fetch_inputs()
    # Stages that REUSE the last scrape instead of re-fetching suppliers: `catalog` (consolidate only) and
    # `republish` (re-run the pipeline + catalog from cache, e.g. to release products a backbone-leaf
    # approval just made valid). Every other stage pulls fresh supplier data first.
    if stage not in _NO_SCRAPE_STAGES:
        if (rc := _live_scrape(sources)) != 0:
            log.error("produce aborted: live scrape failed",
                      extra={"extra_fields": {"rc": rc, "sources": sources or "all"}})
            return rc
    rc = build.main(argv)
    _finalize_control_plane(stage)
    if stage in _CATALOG_STAGES:
        from stone_pipeline.ledger import writethrough
        if writethrough.enabled():
            # Cold-start stall guard (fail loud, not silent): if nothing typed, the whole catalogue is
            # held untyped and no product will ever serve. Check REGARDLESS of rc -- a typed-0 run can even
            # pass the consistency gate (0 products to check) and look clean. This is fatal, not a hold.
            if (stall := _cold_start_stall()):
                log.error("produce FATAL: cold-start stall -- " + stall)
                print("produce FATAL: cold-start stall -- " + stall)
                return rc or 1
            # A catalog-gate failure that is purely new-this-run varieties is the expected two-pass
            # checkpoint, not an error -- reconcile against the ledger so pass-1 exits 0.
            if rc != 0:
                return _reconcile_gate(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
