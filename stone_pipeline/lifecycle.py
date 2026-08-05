"""The one entity-lifecycle module: every add/remove operation for BOTH sources and variations, over a
single guarded operations layer. This replaces the per-verb duplication that used to live in
config/runner.py (the same lock + run-guard + resolve + translate block copied across delist/purge/reset/
remove). One `_exclusive` guard and one `_resolve` are the shared plumbing; each verb is then a few lines.

Two mutual-exclusion layers, kept distinct:
  - process level (`_exclusive`): a destructive lifecycle op, a produce run, and another lifecycle op are
    mutually exclusive in this control-plane process. A run is tracked by config.runner; this module owns
    the destructive-op flag. Cross-checks are lazy imports (runner <-> lifecycle) to avoid an import cycle.
  - ledger level (`sync._lock_and_check_in_flight`, raised as ServeInFlight): a pull holds a lease -> the
    op refuses atomically inside the DB transaction. Mapped to 409 here.

Source verbs: pause / resume (config only), delist / purge / reset / remove_source (ledger), clean (fs).
Variation verbs: retire_variation / un_retire (added in the variation-retire phase).
No em dashes (design principle 2).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from stone_pipeline.core import logfmt

log = logfmt.get_logger("lifecycle")

_lock = threading.Lock()
_active: str | None = None      # the in-flight DESTRUCTIVE lifecycle op (delist/purge/reset/remove/clean),
                                # or None. Runs are tracked separately by config.runner (status-based); the
                                # two never nest their locks: start_run checks active() BEFORE taking its
                                # lock, and an op checks runner.run_in_progress() AFTER claiming here.


class _Busy(Exception):
    """A precondition that maps to HTTP 409 (something else holds the slot, or an entity is still live)."""


class _NotFound(Exception):
    """A missing target that maps to HTTP 404 (an unknown variation key)."""


# -- shared exclusion ----------------------------------------------------------

def _try_claim(name: str) -> str | None:
    global _active
    with _lock:
        if _active:
            return _active
        _active = name
        return None


def _release(name: str) -> None:
    global _active
    with _lock:
        if _active == name:
            _active = None


def active() -> str | None:
    """The in-flight destructive lifecycle op, or None. config.runner.start_run checks this to refuse a
    run while an op is mid-flight (checked before it takes its own lock, so the locks never nest)."""
    with _lock:
        return _active


def _load_pristine_seed_keys() -> set[str]:
    """The Key set of the committed CLEAN seed, read from the PRISTINE copy baked read-only at image build
    (`variants_export_base.seed.csv`). The live base self-mutates at runtime (`base := 1_variants_full`
    each produce), so it is not a trustworthy clean source; the pristine copy is. Falls back to the live
    base only when the pristine copy is absent (local dev, where the committed base IS pristine). Empty set
    if neither exists -- the reconcile then still dedups, just without a seed-Key survivor preference."""
    import csv
    from stone_pipeline.config.settings import SETTINGS
    seed = SETTINGS.paths.variants_export_base_seed_csv
    if not seed.exists():
        seed = SETTINGS.paths.variants_export_base_csv
    if not seed.exists():
        log.warning("no committed seed to reconcile the variation table against; dedup runs seedless")
        return set()
    with seed.open(encoding="utf-8-sig", newline="") as handle:
        return {(r.get("Key") or "").strip() for r in csv.DictReader(handle) if (r.get("Key") or "").strip()}


def _load_pristine_seed_identities() -> set[tuple]:
    """The variety-IDENTITY set (branch,type,name) of the committed clean seed, so a pristine reconcile can
    drop any ledger variation whose identity is NOT in the seed -- a test mint / Medusa-only leftover the
    export union keeps re-importing -- and tombstone it for the removals pull. Identity-keyed (not raw Key),
    so a legacy Key and a seed Key for the SAME variety share an identity and a real variety is never dropped
    for a key mismatch. Same seed source as _load_pristine_seed_keys; empty set (no source) -> no stale drop."""
    import csv
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.reference.loaders import variety_identity
    seed = SETTINGS.paths.variants_export_base_seed_csv
    if not seed.exists():
        seed = SETTINGS.paths.variants_export_base_csv
    if not seed.exists():
        return set()
    with seed.open(encoding="utf-8-sig", newline="") as handle:
        return {variety_identity((r.get("Key") or "").strip(), (r.get("Name") or "").strip())
                for r in csv.DictReader(handle) if (r.get("Key") or "").strip()}


def _reseed_base_from_pristine(seed_path=None, base_path=None) -> dict:
    """Factory reset: reset the LIVE base FILE to the pristine committed seed -- locally AND on S3 -- so the
    next produce truly derives from the seed. The reset already reconciles the LEDGER to the seed, but the
    live variants_export_base.csv self-mutates each produce (base := 1_variants_full) and is republished to
    S3 (the S0 single-source-of-truth); without also reseeding that file, a factory reset would leave the
    drifted base in place and the 'cold start' would silently start from it, not the seed. Best-effort +
    loud: a failure never fails the reset (the ledger/config are already reset) but is surfaced."""
    import shutil
    from pathlib import Path
    from stone_pipeline.config.settings import SETTINGS
    seed = Path(seed_path or SETTINGS.paths.variants_export_base_seed_csv)
    base = Path(base_path or SETTINGS.paths.variants_export_base_csv)
    if not seed.exists():
        log.warning("reset: no pristine base seed baked; leaving the base file as-is (local dev)")
        return {"reseeded": False, "reason": "no pristine seed"}
    try:
        shutil.copyfile(seed, base)                     # local base := pristine seed
        from stone_pipeline.reference.sync_variants_base import publish_base_to_import, publish_base_to_s3
        published = publish_base_to_s3(base)            # S3 base := pristine seed, so the cold start reads it
        # ALSO publish the seed to the import namespace (to_upload/) so Medusa's bulk-restore reads the SEED.
        # Only the pristine reset writes there, so to_upload/ is pinned to the seed (never the grown base) --
        # a 'Restore Medusa to base' always restores to seed. Best-effort, same as the from_medusa/ publish.
        import_published = publish_base_to_import(base)
        log.info("reset: base file reseeded from the pristine seed",
                 extra={"extra_fields": {"s3_published": published, "import_published": import_published}})
        return {"reseeded": True, "s3_published": published, "import_published": import_published}
    except Exception as exc:
        log.exception("reset: base-file reseed FAILED; the base may still be the drifted copy")
        return {"reseeded": False, "error": str(exc)}


_snapshot_on_mutation = False   # set True ONLY by the config server (serve); off in tests, laptop, and the
                                # produce subprocess, so a lifecycle op there never attempts a real S3 call.


def enable_config_snapshots() -> None:
    """The config server calls this at startup so a lifecycle mutation snapshots config.db immediately (on
    top of the periodic + atexit snapshot). Everywhere else it stays off -- no S3 on a plain store write."""
    global _snapshot_on_mutation
    _snapshot_on_mutation = True


def _snapshot_config() -> None:
    """Best-effort: snapshot config.db to S3 right after a lifecycle mutation, so a pause/delist/remove is
    durable even if the task crashes before the periodic snapshot. Only inside the config server (guarded),
    and best-effort there (a snapshot failure never fails the op)."""
    if not _snapshot_on_mutation:
        return
    try:
        from stone_pipeline.config import store
        from stone_pipeline.ledger import snapshot
        snapshot.save_config(store.config_db_path())
    except Exception:
        log.debug("config snapshot after a lifecycle op skipped", exc_info=True)


@contextmanager
def _exclusive(name: str):
    """Hold the destructive-op slot for the block. Raises _Busy if another op holds it, or (after
    claiming) if a produce run is in progress -- so a run and an op are mutually exclusive without the
    two locks ever nesting (claim, then check the run, then release on the way out)."""
    busy = _try_claim(name)
    if busy:
        raise _Busy(f"a {busy} is in progress")
    try:
        from stone_pipeline.config import runner
        if runner.run_in_progress():
            raise _Busy("a run is in progress; refusing to run a lifecycle op mid-run")
        yield
    finally:
        _release(name)


def _resolve(sources, *, require_non_empty: bool = True):
    """Validate + translate a sources request ONCE against the CONFIG STORE -- the single authority for
    'does this source exist'. Every lifecycle op (reset / purge / delist / remove) resolves identically,
    so a config-only source (one with no coded adapter, e.g. an admin-added or junk row) is uniformly
    addressable and removable, with no per-endpoint special case. The adapter registry stays ONLY in
    runner._resolve_sources for RUNNING -- you cannot scrape without an adapter, which is a genuinely
    separate capability check, not a second identity layer. Returns (names, codes, None) on success, or
    (None, None, (error, status)) to return directly.
      names  = the requested source names (all exist in the store; an unknown one is a 404, never dropped).
      codes  = their source_codes (SKU prefixes) for ledger scoping.
      None sources -> global scope (names=[], codes=None) when require_non_empty is False."""
    from stone_pipeline.config import store
    if not sources:
        if require_non_empty:
            return None, None, ({"error": "requires an explicit sources list"}, 400)
        return [], None, None
    rows = store.read_sources()
    unknown = [s for s in sources if s not in rows]
    if unknown:
        return None, None, ({"error": f"unknown source(s): {unknown}"}, 404)
    return list(sources), [rows[s].source_code for s in sources], None


def _ledger_op(name: str, work):
    """Run `work(ledger, sync)` under the exclusive slot + the ledger lease guard, mapping the standard
    failures to http codes. `work` returns the success dict (paired with 200). _Busy -> 409, an in-flight
    pull (ServeInFlight) -> 409, anything else -> 500."""
    from stone_pipeline.ledger import sync, writethrough
    from stone_pipeline.ledger.db import Ledger
    try:
        with _exclusive(name):
            with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as ledger:
                return work(ledger, sync), 200
    except _NotFound as exc:
        return {"error": str(exc)}, 404
    except _Busy as exc:
        return {"error": str(exc)}, 409
    except sync.ServeInFlight as exc:
        return {"error": str(exc)}, 409
    except Exception as exc:
        log.exception("%s failed", name)
        return {"error": str(exc)}, 500


# -- source verbs: freeze/unfreeze (config only, no ledger, no exclusive slot) --

def pause(sources) -> tuple[dict, int]:
    """Freeze a source: stop scraping, products stay live and buyable at their last values. Config-only
    (lifecycle=paused, enabled=0); run_all skips it next run. Reversible via resume."""
    from stone_pipeline.config import store
    names, _codes, err = _resolve(sources)
    if err:
        return err
    for name in names:
        store.set_state(name, lifecycle="paused", enabled=False)
    log.info("source(s) paused (frozen, still live)", extra={"extra_fields": {"sources": names}})
    _snapshot_config()
    return {"paused": names}, 200


def resume(sources) -> tuple[dict, int]:
    """Re-enable a paused OR delisted source (lifecycle=active, enabled=1). A resumed PAUSED source is
    already live; a resumed DELISTED source needs a Run to restock (its qty was zeroed) -- flagged as
    restock_required so the UI can prompt one (fix: paused and delisted are no longer treated the same)."""
    from stone_pipeline.config import store
    names, _codes, err = _resolve(sources)
    if err:
        return err
    restock = [n for n in names if (store.get_row(n) or {}).get("lifecycle") == "delisted"]
    for name in names:
        store.set_state(name, lifecycle="active", enabled=True)
    log.info("source(s) resumed", extra={"extra_fields": {"sources": names, "restock_required": restock}})
    _snapshot_config()
    return {"resumed": names, "restock_required": restock}, 200


# -- source verbs: offline / purge / reset / remove (ledger, exclusive slot) ---

def delist(sources) -> tuple[dict, int]:
    """Stage 1 vendor removal (Take offline): set the sources' products to qty 0 + reason=delisted, and
    lifecycle=delisted + disable so a scrape never silently re-stocks an offline vendor. Reversible."""
    from stone_pipeline.config import store
    names, codes, err = _resolve(sources)
    if err:
        return err
    result, code = _ledger_op("delist", lambda lg, sync: sync.delist_source(lg, source_codes=codes))
    if code != 200:
        return result, code
    for name in names:
        store.set_state(name, lifecycle="delisted", enabled=False)
    log.warning("source(s) taken offline (delist)", extra={"extra_fields": {
        "sources": names, "delisted": result.get("delisted")}})
    _snapshot_config()
    return {**result, "disabled": names}, 200


def purge(sources=None) -> tuple[dict, int]:
    """Dead-stock purge: hard-delete the qty-0 products and record a product tombstone per SKU so Medusa
    deletes the same set. `sources` scopes; None = all. Returns {product, inventory, external_ids}."""
    _names, codes, err = _resolve(sources, require_non_empty=False)
    if err:
        return err
    return _ledger_op("purge", lambda lg, sync: sync.purge_discontinued(lg, source_codes=codes))


def reset(sources=None, hard=False, pristine=False) -> tuple[dict, int]:
    """Clean-start the ledger sync overlay (the coordinated reset). soft re-serves from zero without a
    re-scrape; hard also drops the scraped products AND wipes the hosted product images (scoped -> only the
    named sources' images, global -> all), so a re-scrape regenerates them from scratch (the "spend GPU/FAL"
    restart); soft KEEPS the images. Variation/backbone rows are never deleted; a 'retiring' variety is
    preserved (not un-retired). `sources` scopes; None = global.

    A GLOBAL reset ALSO clears the config.db review queue + operator-pasted attribute ids, so a clean start
    is coherent across BOTH stores in one call: the :4200 queue is blank immediately (not stale until the
    next produce recomputes it) and no dead Medusa id survives the wipe. Durable operator intent
    (mint/reject/alias/seed decisions, retired keys, the approved-leaf overlay) is KEPT. A scoped per-source
    reset leaves config.db alone, matching how it leaves the ledger's shared base layer alone.

    `pristine` (factory reset) is the TRUE cold start: on top of a global hard reset it ALSO (a) wipes the
    durable operator overlay (variety_decision + backbone_leaf_decision + retired_variation), so the next
    produce derives the catalog PURELY from the committed seed (variants_export_base + backbone_*), with no
    accumulated mint/alias/approve/retire re-applying, and (b) PRUNES stale variations (in_full=0, no
    products) -- tombstoning re-key old sides and dropped varieties so MEDUSA converges to the seed too,
    not just the sync state. Without (b), a seed change (e.g. a retype) leaves the old variety live in
    Medusa forever (seed vs Medusa out of line). It is global-only (a scoped factory reset is meaningless)
    and forces hard=True (a cold start starts from no scraped products), so it inherits the hard reset's
    GLOBAL image wipe. The cheaper 'clean raw scraped data' (stone_pipeline.clean, a SEPARATE endpoint) and a
    SOFT reset KEEP the images so the manifest reuses them (no GPU/FAL). Variant textures (<env>/variations/)
    are never touched by any of these. Registered sources and run history are left alone -- neither affects
    the catalog output."""
    if pristine and sources is not None:
        return {"error": "pristine (factory) reset is global-only; do not pass sources"}, 400
    if sources is not None and not sources:
        # An EXPLICIT empty list is almost always a UI bug (a global reset+wipe is a big hammer to trigger by
        # accident). A GLOBAL reset is null/omit, a scoped one names sources -- [] is neither, so refuse it.
        return {"error": "sources was an empty list; omit it (or pass null) for a global reset, or name sources"}, 400
    if pristine:
        hard = True                            # a factory cold start begins from zero scraped products
    _names, codes, err = _resolve(sources, require_non_empty=False)
    if err:
        return err
    # pristine = TRUE cold start: reconcile the variation table to the committed seed BEFORE the sync-state
    # reset, so duplicate / re-key OLD SIDES seeded before a seed cleanup are tombstoned (Medusa deletes
    # them on the removals pull) AND dropped locally -- not left to re-render every produce. Read from the
    # PRISTINE seed (baked read-only at image build): the live variants_export_base.csv self-mutates
    # (base := 1_variants_full each produce), so it is not a trustworthy clean source at runtime.
    seed_keys = _load_pristine_seed_keys() if pristine else None
    # The seed's variety IDENTITIES, so the reconcile drops + tombstones any ledger variety not in the seed
    # (a test mint / Medusa-only leftover), bringing Medusa in line with the base on the removals pull.
    seed_identities = _load_pristine_seed_identities() if pristine else None
    # 'not a duplicate' verdicts: a durable correctness override the seed-reconcile must honour, so a variety
    # the operator already confirmed distinct is never re-dropped as a dup on the next pristine.
    protected = None
    if pristine:
        from stone_pipeline.config import decisions_store as _decisions
        protected = _decisions.protected_keys()

    def _do_reset(lg, sync):
        # reconcile (drop + tombstone dup old sides AND non-seed leftovers) BEFORE the sync-state reset, so
        # the tombstones are recorded and the dropped rows are gone before prune/overlay run. Keep
        # reset_sync_state's shape flat (callers read body["reset"]["variation"] etc.); attach as a sibling.
        reseed = (sync.reconcile_variations_to_seed(lg, seed_keys, protected, seed_identities)
                  if (pristine and seed_keys is not None) else None)
        reset_result = sync.reset_sync_state(lg, source_codes=codes, hard=bool(hard), prune_stale=pristine)
        if reseed is not None:
            reset_result["reseed"] = reseed
        # Build the FULL response INSIDE the exclusive slot, so the config.db clear AND the image wipe are
        # covered by the same "no run / other op mid-flight" guard as the ledger reset. A run's start_run
        # checks lifecycle.active(), which stays held until this returns -- closing the window where a produce
        # could start and re-stage images while the wipe is mid-delete (interleaved/partial wipe).
        out = {"mode": "pristine" if pristine else ("hard" if hard else "soft"), "reset": reset_result}
        if codes is None:                          # global reset only -> pair the config.db clean-start
            from stone_pipeline.config import decisions_store, store
            config = {"review_pending": decisions_store.clear_review_pending(),
                      "attribute_ids": decisions_store.clear_attribute_ids()}
            if pristine:                           # factory reset: also forget the durable operator overlay
                config["variety_decisions"] = decisions_store.clear_variety_decisions()
                config["leaf_decisions"] = decisions_store.clear_leaf_decisions()
                config["retired_keys"] = store.clear_retired()
                out["base_reseed"] = _reseed_base_from_pristine()   # S0: the base file itself starts pristine
            out["config"] = config
        if hard:
            # EXPENSIVE restart: a HARD reset ("Remove data (keep config)", and the factory reset which forces
            # hard) wipes the hosted product images (raw scraped + treated improved + markers + manifest), so
            # the next scrape re-downloads and re-processes from scratch -- the "spend GPU/FAL" restart. SCOPED
            # wipes ONLY the named sources' images (products/<folder>/<source>/); GLOBAL wipes all. The cheaper
            # 'clean raw scraped data' (/clean) and a SOFT reset KEEP them, so the manifest reuses the existing
            # processed images (no GPU/FAL). Variant textures (<env>/variations/) are never touched. Best-effort
            # + LOUD: a wipe failure (e.g. missing s3:DeleteObject) never fails the reset (catalog/ledger are
            # already reset) but is surfaced so the operator knows images stayed.
            try:
                from deploy.cleanup_images import wipe_all_product_images, wipe_source_product_images
                out["images_wiped"] = ({s: wipe_source_product_images(s) for s in sources}
                                       if sources else wipe_all_product_images())
            except Exception as exc:
                log.exception("reset: product-image wipe FAILED; images were kept")
                out["images_wiped"] = {"error": f"wipe failed, images kept: {exc}"}
        return out

    return _ledger_op("reset", _do_reset)


def remove_source(name: str) -> tuple[dict, int]:
    """Stage 2 vendor removal (Remove permanently): guarded 409 while the source still has live (qty>0)
    products (take it offline first), then purge its qty-0 products (tombstoned) and delete its config
    row. Returns {removed_source, config_removed, purged, external_ids}.

    ISS-4: removal tombstones the products; to bring the source BACK, re-add it AND run a `reset` (soft)
    before the next produce. A plain re-run + re-pull does NOT rebuild -- the removed products are already
    acked/synced, so nothing re-serves until a reset re-marks them dirty. The re-serve happens via reset,
    never automatically off a re-scrape."""
    from stone_pipeline.config import store
    # ISS-5: resolve via the ONE store-based _resolve (same as reset/purge/delist), so a config-only
    # source (no coded adapter) is removable. Every store row carries a source_code (SourceConfig
    # back-fills it), so `codes` is always the source's SKU prefix. 404 when there is no config row.
    names, codes, err = _resolve([name])
    if err:
        return err

    def work(lg, sync):
        live = sync.live_products(lg, source_codes=codes)
        if live:
            raise _Busy(f"{name!r} still has {live} live product(s); take it offline first")
        return sync.purge_discontinued(lg, source_codes=codes, reason="vendor_removed")

    result, code = _ledger_op("remove_source", work)
    if code != 200:
        return result, code
    config_removed = store.delete_source(name)
    log.warning("source REMOVED (purge + delete config)", extra={"extra_fields": {
        "source": name, "purged": result["product"], "config_removed": config_removed}})
    _snapshot_config()
    return {"removed_source": name, "config_removed": config_removed,
            "purged": result["product"], "external_ids": result["external_ids"]}, 200


# -- variation verbs: retire / un-retire (explicit operator removal of a variety) ---

def retire_variation(key: str, force: bool = False) -> tuple[dict, int]:
    """Explicitly remove a variety (also the old side of a re-key). 404 unknown key; 409 if it still has
    LIVE products from active sources (delist/move them first) unless `force` cascades them. Cascades its
    products (tombstoned) + combinations, then tombstones the variety for Medusa iff it was ever synced,
    and records the Key in the durable exclusion memory so a produce never re-mints it. Reversible via
    un_retire."""
    def work(lg, sync):
        if lg.get("variation", "key", key) is None:
            raise _NotFound(f"unknown variation {key!r}")
        live = sync.variation_live_products(lg, key)
        if live and not force:
            raise _Busy(f"variety {key!r} has {live} live product(s); delist or move them first (or force)")
        return sync.retire_variation(lg, key, force=force)

    result, code = _ledger_op("retire_variation", work)
    if code != 200:
        return result, code
    from stone_pipeline.stages import decisions
    decisions.add_retired(key)          # exclusion memory: never re-mint a retired variety (E8/E10)
    return result, 200


def un_retire(key: str) -> tuple[dict, int]:
    """Reverse a retire (mirrors source resume): clear the exclusion memory so the next produce can
    re-mint the variety, flip a still-'retiring' row back to serving, and clear its pending tombstone so
    Medusa does not delete the re-created variety (E5)."""
    result, code = _ledger_op("un_retire", lambda lg, sync: sync.un_retire_variation(lg, key))
    if code != 200:
        return result, code
    from stone_pipeline.stages import decisions
    decisions.remove_retired(key)
    return result, 200


def not_a_duplicate(key: str) -> tuple[dict, int]:
    """'Not a duplicate' (curation state 2 repair): cancel a dedup/reconcile tombstone that would delete a
    variety the operator confirms is distinct, and record a DURABLE protection so a future seed-reconcile
    never re-drops it. Idempotent: a repeat clears 0 and re-asserts the protection. 404 only if the key is
    unknown AND had no pending tombstone to cancel (nothing to act on)."""
    result, code = _ledger_op("not_a_duplicate", lambda lg, sync: sync.cancel_variation_tombstone(lg, key))
    if code != 200:
        return result, code
    if not result.get("known") and not result.get("tombstone_cleared"):
        return {"error": f"unknown variation {key!r} with no pending tombstone to cancel"}, 404
    from stone_pipeline.config import decisions_store
    decisions_store.add_protected(key)                 # durable: the reconcile dedup never re-drops it
    result["protected"] = True
    return result, 200


def rebuild_curation() -> tuple[dict, int]:
    """Global incremental curation rebuild (curation state 1 repair): reseed the base FILE from the
    committed pristine seed (fixes base-file corruption/drift), then kick a catalog re-derive so every
    source is re-curated from the clean base -- WITHOUT a factory reset's ledger wipe, image wipe, or
    re-scrape. Returns a summary the operator can read. Refuses (409) if a produce/reset is already in
    flight or a pull is mid-serve; Blokport also gates the button on 'no pull running'. Curation is global
    (a variety belongs to the canonical catalog, not a source), so this is not per-source by design."""
    # Guard against a mid-serve pull up front, in the exclusive slot, BEFORE touching the base file (the
    # re-derive that follows mutates the ledger; a base swap racing a live pull is the one combo to block).
    def _guard(lg, sync):
        sync._lock_and_check_in_flight(lg)              # ServeInFlight -> 409 via _ledger_op
        return {"ok": True}
    guard, code = _ledger_op("rebuild_curation", _guard)
    if code != 200:
        return guard, code
    reseed = _reseed_base_from_pristine()
    if not reseed.get("reseeded"):
        # no pristine seed baked (local dev): nothing to rebuild from -- report, do not silently re-derive
        return {"rebuilt": False, "base_reseed": reseed,
                "note": "no committed pristine seed to rebuild from"}, 409
    from stone_pipeline.config import runner
    rec, run_code = runner.start_run(None, "catalog")   # re-derive all sources from the clean base (async)
    return {"rebuilt": True, "base_reseed": reseed, "rederive": rec, "rederive_status": run_code,
            "scope": "global", "pristine_retired": True,
            "note": "base reseeded from the committed seed; catalog re-derive dispatched (watch rederive.run_id). "
                    "Curation is global, so this covers every source."}, 200


def clean(sources=None) -> tuple[dict, int]:
    """Housekeeping: `sources` given -> DELETE those sources' raw scraped data (data/<source>/*) for a
    fresh re-scrape; none -> prune superseded scrapes/runs/orphan images. Base config + ledger untouched.
    Refuses (409) a paused/delisted source (its frozen scrape snapshot is what the catalog rebuilds from
    -- resume first). Mutually exclusive with a run/op, but does no ledger work."""
    from stone_pipeline import clean as clean_mod
    from stone_pipeline.config import store
    try:
        with _exclusive("clean"):
            if not sources:
                return {"pruned": clean_mod.run(dry_run=False)}, 200
            names, _codes, err = _resolve(sources)
            if err:
                return err
            frozen = [n for n in names if (store.get_row(n) or {}).get("lifecycle") in ("paused", "delisted")]
            if frozen:
                return {"error": f"refusing to clean paused/delisted source(s): {frozen}; resume first"}, 409
            return {"deleted_scrapes": clean_mod.delete_source_data(names)}, 200
    except _Busy as exc:
        return {"error": str(exc)}, 409
    except Exception as exc:
        log.exception("clean failed")
        return {"error": str(exc)}, 500
