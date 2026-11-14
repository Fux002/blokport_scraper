"""Full from-scratch produce for the `/run` trigger (and ECS): fetch the Medusa export inputs from
S3, LIVE-scrape the sources, then build (pipeline + catalog / inventory) with ledger write-through.

This is what the config `/run` trigger runs, so a produce on a FRESH host -- a new ECS task with an
empty data/ -- populates the ledger from nothing. The batch run_pipeline.sh does the same fetch ->
scrape -> build; `python -m stone_pipeline.build` stays the laptop path (scrape a source separately,
then build against the local data). The split is deliberate: build assumes data/ is already there,
produce guarantees it. No em dashes (design principle 2).

    python -m stone_pipeline.produce                                  # fetch -> scrape all -> build all
    SCRAPER_RUN_SOURCES=zucchi python -m stone_pipeline.produce        # ...that source only
    SCRAPER_RUN_STAGE=inventory python -m stone_pipeline.produce       # fetch -> scrape -> stock-only refresh
    SCRAPER_RUN_STAGE=catalog python -m stone_pipeline.produce         # NO scrape: just re-consolidate outputs
    SCRAPER_RUN_STAGE=republish python -m stone_pipeline.produce       # NO scrape: re-run pipeline + catalog from cache

SCRAPER_RUN_STAGE / SCRAPER_RUN_SOURCES scope the build (read once in settings.SETTINGS.run); `catalog` and `republish` skip the live scrape
and reuse the last scrape (catalog only consolidates; republish re-runs the whole pipeline + catalog, so
an operator decision like a backbone-leaf approval releases the products it just made valid without a
supplier re-fetch). Ledger write-through is the caller's job (the runner sets it).
"""

from __future__ import annotations

from stone_pipeline import build
from stone_pipeline.core import env, logfmt

log = logfmt.get_logger("produce")
from stone_pipeline.config.settings import IS_PRODUCTION, SETTINGS   # noqa: E402  (log first: settings may fail loud)


# Stages that skip the live supplier fetch and reuse the last scrape in data/.
_NO_SCRAPE_STAGES = ("catalog", "republish")
# Stages that produced a catalog (so the cold-start + consistency reconcile guard applies).
_CATALOG_STAGES = ("catalog", "all", "republish")


def _fetch_inputs() -> None:
    """Pull the current Medusa export (variants_export + attributes) from S3 so the matcher resolves
    existing ids. An EMPTY prefix (a fresh env, nothing published yet), or an export with no rows, is not an
    error: medusa_inputs.fetch_all returns 0 and the cold start proceeds. An export refused as thin that still has rows
    stops the produce in EVERY environment (fetch_inputs raises SystemExit, which this does not catch): left out,
    it would run as a cold start and publish empty valid combinations. An exception is a real S3 / credential
    failure: in production it aborts the produce (matching against a stale or absent export would hold every
    variety and exit 0); in development (a laptop without S3) it logs and continues. A REFUSED attributes.csv (thin or
    malformed: Medusa mid-reset) is no outage and stops the produce in every environment: a produce never runs on a
    vocabulary it knows is stale (G10's attributes half)."""
    from stone_pipeline.io import medusa_inputs
    try:
        medusa_inputs.fetch_all()
    except medusa_inputs.AttributesUnavailable:
        log.exception("attributes.csv refused; a produce never runs on a stale vocabulary")
        raise SystemExit(2)
    except Exception:
        if IS_PRODUCTION:
            log.exception("fetch_inputs FAILED in production; refusing to produce against a stale or absent export")
            raise SystemExit(2)
        log.warning("fetch_inputs skipped; matching runs against whatever export is local",
                    exc_info=True)
    # The name-field kinds have NO local fallback on any environment: every writer cleans a name by them, so a
    # produce that cannot fetch the map stops loud rather than write a name on a guessed kind.
    try:
        from stone_pipeline.io import medusa_inputs
        medusa_inputs.fetch_field_kinds()
    except Exception:
        log.exception("field-kinds.json could not be fetched; a produce never writes names on a guessed kind")
        raise SystemExit(2)
    # The operator's manual overrides: S3 is their authority on every environment, so a produce that cannot fetch
    # them stops loud rather than run on a stale or unvouched copy (none on S3 = none, as before).
    try:
        from stone_pipeline.io import medusa_inputs
        medusa_inputs.fetch_overrides()
    except Exception:
        log.exception("manual_overrides.csv could not be fetched or is malformed; a produce never runs on unvouched "
                      "overrides")
        raise SystemExit(2)
    # Realign the category registry with the export NOW on disk. The pcats were read once at import
    # (bootstrap, BEFORE this fetch); a produce that just pulled a changed attributes.csv -- a new Medusa
    # pcat id or a corrected category label -- would otherwise keep gating emit + new-variety fan-out on
    # the stale import snapshot (symptom: every row category_invalid). Runs even when the fetch was
    # skipped (re-reads the same local file the import used), so it can only ever correct, never regress.
    from stone_pipeline.config import categories
    categories.refresh_category_pcats()


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


def _scrape_stamp_now() -> str:
    """Now, in the form the product scraper stamps its run folders with."""
    from datetime import datetime, timezone
    from scrapers.base import RUN_STAMP_FORMAT
    return datetime.now(timezone.utc).strftime(RUN_STAMP_FORMAT)


def _emit_supplier_prices(sources: list[str] | None, since: str) -> None:
    """Deliver the supplier prices this produce's own scrape read (supplier_prices.run), isolated from the products: a
    failure is logged loudly and never fails the product produce, whose products carry no price."""
    from botocore.exceptions import BotoCoreError, ClientError
    from stone_pipeline.config.settings import BRAND, ENV_NAME, S3_BUCKET
    from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
    from supplier_prices import run
    from supplier_prices.config import RUNS_DIR
    try:
        client = None if SETTINGS.s3.dry_run else aws_client("s3", config=BOTOCORE_DEFAULTS)
        delivered = run.deliver(sources, since, data_dir=SETTINGS.paths.data_dir, out_root=RUNS_DIR / ENV_NAME,
                                client=client, bucket=S3_BUCKET, brand=BRAND)
    except (ClientError, BotoCoreError, OSError) as exc:
        log.error(f"supplier prices not delivered: {type(exc).__name__}: {exc}",
                  extra={"extra_fields": {"sources": sources or "all"}})
        return
    for run_line in delivered:
        log.info("supplier prices delivered", extra={"extra_fields": run_line})


# The catalog consistency gate keys off variants_export.csv, so it FALSE-ALARMS on the two-pass pull
# checkpoint: (1) varieties minted THIS produce have no Medusa id yet ("NOT in the current export" /
# "NO valid-combination row"), and (2) a PRISTINE cold start has an EMPTY export, so the gate cannot
# verify anything against it ("cannot verify") -- the same condition tree_build already treats as a soft
# cold start (it writes empty combinations so the variants flow to Medusa). All three are rescued ONLY
# when the ledger confirms new variations explain them (held > 0); any error OUTSIDE this class, or
# held == 0 (e.g. a warm run whose export genuinely broke, with nothing new), keeps the gate fatal.
_COLD_START_MARKERS = ("NOT in the current export", "NO valid-combination row", "cannot verify")


def _mirror_archived() -> None:
    """VE2: mirror the export's archived flag onto the ledger RIGHT AFTER the fetch, before any record_source
    write, so a variety Blokport archived in the fresh export is frozen for the whole produce (every stage,
    `scrape` included), not only from the later catalog step. No-op when write-through is off.

    It RAISES on a ledger failure (no swallow): the ledger is the live sync source, and a failed
    record_source/record_catalog already aborts the run. A silently-skipped mirror would let record_source
    (which runs a moment later, per source) serve new products on a freshly archived variety for the whole
    scrape -- exactly the hole this call closes -- so it must fail loud, not continue."""
    from stone_pipeline.ledger import writethrough
    if not writethrough.enabled():
        return
    with writethrough.open_ledger() as ledger:
        n = writethrough.mirror_archived_variations(ledger)
    log.info("VE2 archived mirror after fetch", extra={"extra_fields": {"archived_changed": n}})


def _migrate_learned_aliases() -> None:
    """P2-10 C2: move state/alias_writeback.csv into config.db learned_alias, ONCE per env (the scraper manager's
    ruling 11-12), before any stage can match. It resolves each record through the ledger, so it runs where the
    ledger is the live sync source (write-through on); a later produce finds the marker and returns at once. A
    failure RAISES: a produce must not match with the learned aliases half moved."""
    from stone_pipeline.ledger import writethrough
    if not writethrough.enabled():
        return
    from stone_pipeline.aliases import migrate_learned
    migrate_learned.run_once()


def _frozen_waiting() -> int:
    """New products waiting on a frozen (archived) variety -- counted from the ledger for a visible log line."""
    from stone_pipeline.ledger import sync, writethrough
    with writethrough.connect(bind=True) as lg:
        return sync.frozen_waiting_products(lg)


def _log_frozen_waiting() -> None:
    """VE2: surface new products held on a frozen (archived) variety. They wait silently as pending (the sync
    lane never serves them), so the produce logs the count on EVERY stage that recorded products (scrape
    included), not only the catalog stages. The VISIBLE upstream hold (curate refusing the new bind) is PR5's.
    No-op when write-through is off."""
    from stone_pipeline.ledger import writethrough
    if not writethrough.enabled():
        return
    if (waiting := _frozen_waiting()):
        log.warning("VE2: new products are held on a frozen (archived) variety and will not sync until "
                    "the variety is active again or the product is moved off it",
                    extra={"extra_fields": {"frozen_waiting": waiting}})


def _ledger_gate_state() -> tuple[int, int]:
    """(held, untyped) from the ledger -- the pull-model source of truth (NOT the stale CSV export),
    which is why pass-2 needs no refreshed Medusa export. The schema queries live in the ledger layer
    (sync.gate_state); produce owns only the held-vs-fatal DECISION made from them."""
    from stone_pipeline.ledger import sync, writethrough
    with writethrough.connect(bind=True) as lg:
        return sync.gate_state(lg)


def _cold_start_stall() -> str | None:
    """A reason string when this produce typed NOTHING it can serve, else None. Typing is a hard serve
    gate, so an empty type vocabulary (attributes export absent/unseeded) or 0 typed of a non-empty
    produced set means the WHOLE catalogue is held untyped forever -- a broken cold start, not the
    expected pass-1 hold. produced == 0 (a no-op re-run) is not a stall."""
    from stone_pipeline.ledger import sync, writethrough
    with writethrough.connect(bind=True) as lg:
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
    exit 0 reporting the held count; else stay fatal. ONLY the gate's own exit code is ever reconciled: a
    write-through abort, a scrape floor or a stall carries another code and stays fatal even when the
    consistency errors are the cold-start class and new varieties are held (the normal state of any
    new-variety produce, so never an excuse for an unrelated failure)."""
    from stone_pipeline import catalog as catalog_mod
    if rc != catalog_mod.CONSISTENCY_GATE_RC:
        return rc
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
        from stone_pipeline.config.control import admission, diagnostics
        diagnostics.persist_from_outputs()
        admission.evaluate_from_outputs()
    except Exception:
        log.exception("control-plane finalize (diagnostics/admission) failed; non-fatal")


def run(sources: list[str] | None, stage: str, verify: bool = False) -> int:
    """A full produce (fetch export inputs -> live scrape -> build -> persist + publish) scoped to `stage`
    and `sources` (None -> every enabled source). The param-driven core: the __main__ entry calls it with
    the values from SETTINGS.run; no argv. `verify` re-checks only (no fetch/scrape), delegating to build."""
    # verify is a pure re-check of what is already built: no fetch, no scrape -- delegate straight.
    if verify:
        return build.verify_gate()

    if stage not in build.STAGES:
        return build.run(sources, stage)   # let build report the unknown stage consistently

    _fetch_inputs()
    _mirror_archived()   # VE2: freeze archived varieties from the FRESH export before any source is recorded
    _migrate_learned_aliases()
    # Stages that REUSE the last scrape instead of re-fetching suppliers: `catalog` (consolidate only) and
    # `republish` (re-run the pipeline + catalog from cache, e.g. to release products a backbone-leaf
    # approval just made valid). Every other stage pulls fresh supplier data first.
    if stage not in _NO_SCRAPE_STAGES:
        since = _scrape_stamp_now()     # this produce's scrape folders are stamped at or after it
        if (rc := _live_scrape(sources)) != 0:
            log.error("produce aborted: live scrape failed",
                      extra={"extra_fields": {"rc": rc, "sources": sources or "all"}})
            return rc
        _emit_supplier_prices(sources, since)
    rc = build.run(sources, stage)
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
                rc = _reconcile_gate(rc)
    # VE2: log products held on a frozen variety on EVERY stage that recorded products (scrape writes new
    # products too), so it is not confined to the catalog-only branch above. ONLY on a clean build: it must
    # never run its own ledger query in place of a failing build's exit code (a gate rc, an abort), so a
    # build failure returns untouched.
    if rc == 0:
        _log_frozen_waiting()
    return _persist_and_publish(stage, rc)


def main() -> int:
    """The __main__ entry: read the run scope from the env ONCE (SETTINGS.run), then run. No argv."""
    cfg = SETTINGS.run
    return run(list(cfg.sources) or None, cfg.stage, cfg.verify)


def _persist_and_publish(stage: str, rc: int) -> int:
    """The last two steps of a SUCCESSFUL produce, owned here so every caller (the config runner's /run,
    the batch task) gets them: snapshot the scrape-artifact trees (outputs/ + data/) so a fresh task restores
    the last scrape instead of finding nothing to consolidate; then, for a catalog-producing stage, publish
    the deliverables (to_upload/ + review/, with the manifest) to S3 so Blokport's importer sees THIS
    produce's fixed keys. A publish failure FAILS the produce: the build completed, but a produce whose
    deliverables never reached S3 is not a success (Blokport would pull the previous set as this one)."""
    if rc != 0:
        return rc
    from stone_pipeline.ledger import snapshot
    snapshot.save_artifacts()
    snapshot.save_state()       # persist the learned-alias write-back so a roll does not re-review spellings
    if stage in _CATALOG_STAGES:
        from stone_pipeline.io import deliverables
        try:
            deliverables.upload_all(env.getenv("BLOKPORT_RUN_ID"))
        except Exception:
            log.exception("produce FAILED: deliverables not published to S3 (the build itself completed)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
