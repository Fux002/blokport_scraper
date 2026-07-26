"""Catalog sync CLI: consolidate the new variants, aliases, backbone entries, and
images from EVERY source run into one shared catalog folder, with a sync
checklist. The catalog (variants/backbone/tree/images) is shared across sources;
products are per-source. Run this after scraping, before importing products.

    python -m stone_pipeline.build        # scrape + catalog + combinations + consistency gate
    # (or the stages individually: `run all` then `catalog` -- catalog now builds the combinations
    #  itself and runs the consistency gate, so `tree` is no longer a separate manual step.)
    # then follow to_upload/SYNC_STEPS.md

It reads every run's diagnostics/canonical.parquet, so it reflects all sources at
once and de-duplicates a variant that several suppliers carry.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.layout import write_sync_md
from stone_pipeline.core.text import looks_code_shaped
from stone_pipeline.io import staging
from stone_pipeline.reference import loaders
from stone_pipeline.stages import build_tile_backbone, curate, emit, emit_catalog, tree_build

log = logfmt.get_logger("catalog")

# Per-image-script wall-clock ceiling. Generation (FLUX over the whole queue, 4 workers) legitimately runs
# minutes; this is a defense-in-depth backstop far above normal so a genuinely wedged stage (e.g. a hung
# model load or network call) fails and flags its variants HELD instead of pinning the whole Batch task.
IMAGE_SCRIPT_TIMEOUT_S = 3600


def latest_run_dirs(outputs_root: Path) -> list[Path]:
    """The NEWEST run folder per source under outputs_root. A run id is `<source>_<date>_<time>`,
    so the lexically-greatest folder for a source is its latest scrape; older folders are stale.
    Consolidation MUST ignore the stale ones -- each scrape is a full snapshot, so including two
    run folders for one source would double-count it (duplicate SKUs). This makes the catalog
    idempotent and leftover-proof: a forgotten old run folder can never inflate the output."""
    latest: dict[str, Path] = {}
    for d in sorted(Path(outputs_root).glob("*_*_*")):   # <source>_<YYYYMMDD>_<HHMMSS>
        if d.is_dir():
            latest[d.name.rsplit("_", 2)[0]] = d          # sorted asc -> newest wins
    return list(latest.values())


def find_canonical(outputs_root: Path) -> list[Path]:
    return [c for d in latest_run_dirs(outputs_root)
            if (c := d / "diagnostics" / "canonical.parquet").exists()]


def run(outputs_root: Path | None = None) -> Path:
    outputs_root = Path(outputs_root or SETTINGS.paths.outputs_dir)
    parquets = find_canonical(outputs_root)
    if not parquets:
        raise SystemExit(f"no source runs found under {outputs_root} (run `python -m stone_pipeline.run all` first)")

    rows = []
    sources: set[str] = set()
    for p in parquets:
        src_rows = staging.read_canonical(p)
        rows.extend(src_rows)
        sources.update(r.src_site for r in src_rows)   # union ALL src_sites, not just the first row's

    ref = loaders.load_all()
    result = curate.run(rows, ref)                # -> to_upload/1_variants_update.csv, review/, backbone_additions/
    products = collect_products(outputs_root)     # -> to_upload/3_products_*.csv (per source + combined)
    # Regenerate every mirror backbone (Tiles) from the CURRENT slab backbone before anything reads it:
    # emit_catalog's tile mirror rows (below) and tree_build's combinations both consume the tile backbone,
    # so rebuilding it here makes tiles a pure derivation of slabs that can never silently rot. (The manual
    # build_tile_backbone step used to be forgotten, leaving polluted tiles inconsistent with cleaned slabs.)
    build_tile_backbone.build()
    # Tiles are the SAME stone as their slab but have no product/texture of their own, so their {Key}.png is
    # never generated. Copy each slab's image to its tile Key BEFORE emit reads the S3 key set, so tiles
    # advertise an image instead of being emitted imageless (was ~12k blank tiles).
    mirror_slab_images_to_tiles()
    emit_catalog.build()                          # -> to_upload/1_variants_full.csv -- AFTER products, so its
                                                  #    product-backed image stamping reads THIS run's products
                                                  #    (their variation ids), never a stale prior set
    inventory = collect_inventory(outputs_root)   # -> to_upload/4_inventory_update.csv (existing stock only)
    discontinued = collect_discontinued(outputs_root)  # -> review/4 delete-loop report (stock-0'd in inventory)
    sync = write_sync_md(counts=result.counts, sources=sorted(set(sources)),
                         products=products, inventory=inventory, discontinued=discontinued)
    images_queued = _auto_queue_images()          # new variants -> prompts_to_generate.json (auto)
    # Give each colour-less variety a real colour read from its REAL PRODUCT IMAGE (the de-watermarked
    # scraped photo, keyed by variation_key), then propagate to mirrors, so a no-colour source like
    # varsha/zucchi never nulls colour_id; 'Natural' only if no photo. NOT the generated variant icon --
    # an icon can be a stale/placeholder render that diverges from the actual stone.
    from stone_pipeline.stages import variety_color
    from stone_pipeline.config.settings import CATEGORIES
    _additions = sorted((SETTINGS.paths.catalog_source_dir / "backbone_additions").glob("*.json"))
    _merged = [c.backbone_path for c in CATEGORIES]   # read-only: lets a new tile/block mirror inherit
    product_images: dict[str, str] = {}                # variation_key -> its product photo (first wins)
    for r in rows:
        if r.variation_key and r.image_keys and r.variation_key not in product_images:
            product_images[r.variation_key] = r.image_keys[0]
    color_stats = variety_color.fill_colors(backbone_paths=_additions, reference_paths=_merged,
                                            product_images=product_images)
    to_delete = write_variants_to_delete()         # surface junk variants (bare-code + mis-typed) for deletion
    migrated = migrate_retyped_variant_images(ref) # a re-typed variant keeps its image at its new Key
    held = gate_on_images()                        # AFTER migration: hold only the genuinely-imageless new
    # ONE list: the committed seed of truth is exactly the freshly-gated full upload (base == full).
    from stone_pipeline.reference import sync_variants_base
    sync_variants_base.sync()
    pruned = prune_superseded_runs(outputs_root)  # leave only the latest run folder per source
    # Build the valid combinations HERE, in the same step as the variants/products, so they are
    # always derived from the SAME export. They can never go stale by someone forgetting to run
    # `tree` separately after the export changed (which silently shipped stale combinations before).
    tree_build.run()                              # -> to_upload/2_valid_combinations.csv
    log.info("catalog consolidated", extra={"extra_fields": {
        "sources": sorted(set(sources)), **result.counts,
        "products": sum(products.values()), "inventory": inventory,
        "discontinued": discontinued, "pruned_stale_runs": pruned,
        "images_queued": images_queued, "variant_colors": color_stats,
        "variants_held_no_image": held, "variants_to_delete": to_delete,
        "retyped_images_migrated": migrated}})
    # Reflect the produced 1_variants_full onto the ledger variation table (flag-gated). This runs
    # BEFORE the consistency gate on purpose: in the pull model the ledger is the source of truth Medusa
    # pulls, and a produce that mints new varieties trips the CSV gate ("not in the current export")
    # even though those variations are valid and simply awaiting their first pull. The ledger must carry
    # them regardless; the sync engine gates their products on the variation being synced, and produce
    # reconciles the gate against the ledger (held vs fatal). Inert unless BLOKPORT_LEDGER_WRITETHROUGH.
    if os.environ.get("BLOKPORT_LEDGER_WRITETHROUGH", "").strip().lower() in ("1", "true", "yes", "on"):
        from stone_pipeline.ledger import writethrough
        writethrough.record_catalog()

    # Deterministic consistency gate: fail loudly if the upload set is internally inconsistent
    # (stale/out-of-order combinations or products vs the current export) -- no manual/AI check. In the
    # pull model produce reconciles this against the ledger: new-variety failures are the expected
    # two-pass checkpoint (held), a genuine structural fault stays fatal.
    errors, warnings = verify_consistency()
    for w in warnings:
        log.warning("consistency warning", extra={"extra_fields": {"warning": w}})
    if errors:
        for e in errors:
            log.error("consistency gate FAILED", extra={"extra_fields": {"error": e}})
        raise SystemExit("catalog consistency gate FAILED -- inconsistent upload set:\n  - "
                         + "\n  - ".join(errors))

    return sync


def _auto_queue_images() -> int:
    """Auto-build the generator's prompt queue (image_pipeline/prompts_to_generate.json) for the
    new variants this run, then -- if FAL_KEY is set -- kick off generation so the image is ready on
    S3 before import. Without FAL_KEY it just leaves the queue ready for the image_pipeline run.
    The variant Image URL already points at dev/variations/{Key}.png, so generation overwrites that
    exact object (one image per variant)."""
    import json
    import os

    from stone_pipeline.stages import image_prompts
    try:
        prompts_path = image_prompts.build()
    except Exception:
        log.exception("image prompt queue build failed")
        return 0
    items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    items = items if isinstance(items, list) else items.get("items", [])
    # The generators need the FLUX/BEN2 stack (fal_client + torch), which the lean deployed images
    # deliberately omit -- texture generation is an external/GPU step, and the produce's job here is to
    # QUEUE the prompts (prompts_to_generate.json). Only run inline when BOTH FAL_KEY and the deps are
    # present (a dev box / a dedicated generation image); otherwise queue and say so, rather than run
    # scripts that would just ImportError.
    import importlib.util

    from stone_pipeline.prepare_variant_images import GEN_DEPS   # single source of the generator dep list
    missing_deps = [m for m in GEN_DEPS if importlib.util.find_spec(m) is None]
    generated_inline = False
    if items and os.environ.get("FAL_KEY") and not missing_deps:
        log.info("auto image generation: FAL_KEY + deps present, generating", extra={"extra_fields": {"queued": len(items)}})
        failed = _generate_queued_images()
        generated_inline = True
        if failed:
            # distinct error-level signal: generation failed, so some {Key}.png will be absent. The
            # image gate downstream HOLDS imageless variants, but only when S3 is reachable -- surface
            # this loudly so a wholesale failure isn't mistaken for a clean run.
            log.error("image generation failed -- expected images may be missing from this build",
                      extra={"extra_fields": {"failed_scripts": failed, "queued": len(items)}})
    elif items and os.environ.get("FAL_KEY") and missing_deps:
        # FAL_KEY is set but this image can't generate (no fal_client/torch): queue only, delegate the
        # actual generation to the on-demand GPU texture job (auto-texture, below). Their products stay
        # HELD until {Key}.png exists. Warn (not error) -- this is the expected lean-image path.
        log.warning("%d new variant texture(s) QUEUED (image_pipeline/prompts_to_generate.json) but NOT "
                    "generated here: this image lacks %s. Dispatching the GPU texture job (auto-texture); "
                    "until it lands, their products stay HELD.",
                    len(items), ", ".join(missing_deps), extra={"extra_fields": {"queued": len(items)}})
    elif items:
        # loud signal (risk 2): textures were queued but FAL_KEY is not set for the produce, so they are
        # NOT generated here -- every one of these new variants' products stays HELD out of the catalogue
        # until its {Key}.png exists. At cold-start scale that is thousands of products silently unserved,
        # so warn (not info) rather than let an empty product lane look like a clean run.
        log.warning("no FAL_KEY for the produce: %d new variant texture(s) queued but NOT generated -- "
                    "their products stay HELD until the images exist (wire FAL_KEY, or run image_pipeline)",
                    len(items), extra={"extra_fields": {"queued": len(items)}})
    # Part of the produce (Republish), NOT a cron: when textures were queued but this lean image did not
    # generate them, fire-and-forget ONE on-demand GPU job to do it. No-op unless BLOKPORT_AUTO_TEXTURE is
    # set; best-effort (never fails the produce). The next produce stamps the images (one-cycle hold).
    if items and not generated_inline and SETTINGS.auto_texture.enabled:
        import uuid

        from stone_pipeline.prepare_variant_images import (PROMPTS_LOCAL, TEXTURE_QUEUE_PREFIX,
                                                           publish_prompts)
        from deploy.texture_trigger import submit_texture_job
        dispatched = False
        try:
            # Publish THIS produce's queue to a UNIQUE key so a concurrent produce cannot clobber the queue a
            # still-starting GPU job is about to pull; then dispatch a job bound to exactly that key. Gate the
            # dispatch on a successful publish: if publish no-op'd (S3 dry-run/unreachable) we must NOT submit
            # a job that would pull a stale/absent queue.
            queue_key = f"{TEXTURE_QUEUE_PREFIX}{uuid.uuid4().hex}.json"
            if publish_prompts(PROMPTS_LOCAL, key=queue_key):
                dispatched = bool(submit_texture_job(len(items), queue_key=queue_key))
            else:
                log.error("texture queue publish no-op'd (S3 dry-run/unreachable); NOT dispatching a GPU job "
                          "against a stale queue", extra={"extra_fields": {"queued": len(items)}})
        except Exception:
            log.exception("auto-texture trigger failed (non-fatal; textures stay queued for the next run)")
        if not dispatched:
            # loud + actionable, never swallowed: these variants' products stay HELD until a produce
            # successfully publishes the queue AND submits the GPU job.
            log.error("%d new variant texture(s) queued but NO GPU job was dispatched; their products stay "
                      "HELD until a successful produce publishes + dispatches (check FAL_KEY / S3 write / "
                      "GPU queue+jobdef config)", len(items), extra={"extra_fields": {"queued": len(items)}})
    return len(items)


def _generate_queued_images() -> list[str]:
    """Run the existing image_pipeline chain (FLUX.2 max -> BEN2 -> S3) on the queued prompts.
    Reuses the committed scripts so the S3 step you set up elsewhere stays the single source.
    Returns the list of scripts that exited non-zero (empty == all succeeded)."""
    import subprocess
    import sys
    ip = SETTINGS.paths.workspace_root / "image_pipeline"
    failed: list[str] = []
    for script in ("genetate_images.py", "rb_images.py"):
        if (ip / script).exists():
            # sys.executable (not bare "python") so the venv interpreter is used. -u (unbuffered) so the
            # scripts' per-image progress streams to CloudWatch live: without it Python block-buffers a
            # subprocess' stdout and the whole BEN2 stage looks like a silent hang until it exits. timeout so
            # a wedged stage can never pin the Batch task forever -- a timeout is recorded as a failed script.
            try:
                rc = subprocess.run([sys.executable, "-u", script], cwd=ip, check=False,
                                    timeout=IMAGE_SCRIPT_TIMEOUT_S).returncode
            except subprocess.TimeoutExpired:
                log.error("image-pipeline script timed out",
                          extra={"extra_fields": {"script": script, "timeout_s": IMAGE_SCRIPT_TIMEOUT_S}})
                failed.append(script)
                continue
            if rc != 0:
                log.error("image-pipeline script failed",
                          extra={"extra_fields": {"script": script, "returncode": rc}})
                failed.append(script)
    return failed


def _s3_image_checker():
    """Return a callable Key -> bool reporting whether <env>/variations/{Key}.png exists on S3, or
    None when S3 is unreachable (no boto3/creds -- CI/sandbox) so the gate no-ops there. Backed by a
    SINGLE list_objects_v2 of the variations prefix (one network round-trip TOTAL, shared with
    emit_catalog), not a head_object per Key -- so the cost is O(1) network calls regardless of how
    many variants are checked. The check is READ-ONLY, so it runs even under s3.dry_run."""
    from stone_pipeline.stages.emit_catalog import _s3_variation_keys
    keys = _s3_variation_keys()
    return None if keys is None else (lambda key: key in keys)


def _s3_image_copier():
    """Return a callable (old_key, new_key) -> bool that copies {old}.png to {new}.png in
    <env>/variations/ on S3, or None when S3 is unreachable. A re-typed variant is the SAME stone, so
    its existing image is copied to the new Key rather than regenerated."""
    from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS as _S
    s3 = _S.s3
    try:
        import boto3
        from botocore.config import Config
        cfg = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
        client = boto3.Session(profile_name=s3.credentials_profile or None,
                               region_name=s3.region).client("s3", config=cfg)
    except Exception:
        return None
    prefix = f"{ENV_SEGMENT}/variations/"

    def copy(old: str, new: str) -> bool:
        try:
            client.copy_object(Bucket=s3.bucket, Key=f"{prefix}{new}.png",
                               CopySource={"Bucket": s3.bucket, "Key": f"{prefix}{old}.png"})
            return True
        except Exception:
            return False
    return copy


def _s3_variation_etags() -> dict[str, str] | None:
    """Key -> S3 ETag for every <env>/variations/{Key}.png, via ONE paginated list. None when S3 is
    unreachable. Lets the tile mirror copy a tile ONLY when its slab image actually differs (the tile is
    missing, or the slab was regenerated), never redundantly re-copying an already-in-sync tile."""
    from stone_pipeline.config.settings import ENV_SEGMENT
    s3 = SETTINGS.s3
    try:
        import boto3
        from botocore.config import Config
        cfg = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
        client = boto3.Session(profile_name=s3.credentials_profile or None,
                               region_name=s3.region).client("s3", config=cfg)
        prefix = f"{ENV_SEGMENT}/variations/"
        etags: dict[str, str] = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=s3.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if name.endswith(".png"):
                    etags[name[:-4]] = obj["ETag"].strip('"')
        return etags
    except Exception:
        return None


def mirror_slab_images_to_tiles(etags=None, copier=None) -> int:
    """Copy each slab's image to its tile mirror's Key on S3 so a tile (the SAME stone) advertises an image
    instead of referencing a {Key}.png that no step ever creates. Tiles are mirror COMBINATIONS with no
    product or texture of their own, so ~12k are otherwise emitted imageless (the export's _image_link blanks
    a Key whose object is absent). Join slab<->tile on (stone_type, variant), the identity
    emit_catalog._mirror_rows uses. A tile is (re)copied ONLY when its image differs from its slab's --
    compared by S3 ETag -- so a missing tile is filled, a REGENERATED slab propagates to its tile, and an
    in-sync tile is never re-copied. Idempotent; no-op when S3 is unreachable, so the sandbox is unchanged
    and AWS does it in-build. Must run BEFORE emit_catalog reads the S3 key set, so the freshly-copied tile
    images are advertised (and synced to Medusa) the same run."""
    import json

    from stone_pipeline.config.settings import CATEGORIES, category
    from stone_pipeline.stages.emit_catalog import _posts_of
    tags = etags if etags is not None else _s3_variation_etags()
    copy = copier if copier is not None else _s3_image_copier()
    if tags is None or copy is None:
        return 0
    copied = 0
    for cat in CATEGORIES:
        if not (cat.mirror_of and cat.active):
            continue
        src = category(cat.mirror_of)
        slab_key = {(p.get("stone_type"), p.get("variant")): p["key"]
                    for p in _posts_of(json.loads(src.backbone_path.read_text(encoding="utf-8-sig")))
                    if p.get("key")}
        for mp in _posts_of(json.loads(cat.backbone_path.read_text(encoding="utf-8-sig"))):
            tk = mp.get("key")
            sk = slab_key.get((mp.get("stone_type"), mp.get("variant")))
            if not (tk and sk):
                continue
            se = tags.get(sk)
            if not se or tags.get(tk) == se:
                continue                    # slab image not ready yet, or the tile is already in sync
            if copy(sk, tk):
                tags[tk] = se               # now in sync -> not re-copied next pass
                copied += 1
    log.info("mirrored slab images to tiles", extra={"extra_fields": {"copied": copied}})
    return copied


def migrate_retyped_variant_images(ref, checker=None, copier=None) -> int:
    """When a variant is re-typed (its mis-typed Key is flagged in variants_to_delete and its NAME
    resolves to a different, correct type), copy its S3 image from the old Key to the new correct Key
    -- so the re-typed variant keeps its image instead of being held imageless out of the upload.
    Idempotent (skips when the new Key already has an image); no-op when S3 is unreachable, so the
    local sandbox is unchanged and AWS does it in-build."""
    dele = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    if not dele.exists():
        return 0
    checker = checker if checker is not None else _s3_image_checker()
    copier = copier if copier is not None else _s3_image_copier()
    if checker is None or copier is None:
        return 0
    from stone_pipeline.adapters.tokens import explicit_type_word
    from stone_pipeline.stages.curate import gen_key
    copied = 0
    for r in csv.DictReader(dele.open(encoding="utf-8-sig")):
        old = (r.get("Key") or "").strip()
        name = (r.get("Name") or "").strip()
        if not old or not name:
            continue
        looked = ref.attributes.resolve_id("type", explicit_type_word(name) or name)
        if not looked:
            continue                                   # can't derive the correct type from the name
        new = gen_key(old.split("_")[0], looked[0], name)
        if new != old and checker(old) and not checker(new) and copier(old, new):
            copied += 1
    if copied:
        log.info("migrated re-typed variant images to their new S3 Keys",
                 extra={"extra_fields": {"copied": copied}})
    return copied


def gate_on_images(checker=None, to_upload: Path | None = None, export_file: Path | None = None) -> int:
    """Keep BOTH upload files (1_variants_update + 1_variants_full) to variants that belong there:
      * a genuinely-new variant (not in the Medusa export) whose {Key}.png is NOT on S3 is held out
        until its image exists (the image-on-S3 invariant);
      * a variant flagged in variants_to_delete is dropped so the full/seed file never re-imports
        junk that is on its way out.
    The image check no-ops when S3 is unreachable (local/CI), but the delete-exclusion always runs
    since it needs no S3. Returns the count HELD for missing images."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    export_file = Path(export_file or SETTINGS.paths.export_file)
    upd = to_upload / "1_variants_update.csv"
    if not upd.exists():
        return 0
    dele = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    delete_keys = ({(r.get("Key") or "").strip() for r in csv.DictReader(dele.open(encoding="utf-8-sig"))
                    if (r.get("Key") or "").strip()} if dele.exists() else set())
    export_keys: set[str] = set()
    if export_file.exists():
        with export_file.open(encoding="utf-8-sig", newline="") as h:
            export_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(h)}
    with upd.open(encoding="utf-8-sig", newline="") as h:
        new_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(h)
                    if (r.get("Key") or "").strip() and r["Key"] not in export_keys}
    checker = checker if checker is not None else _s3_image_checker()
    if checker is None and new_keys:
        # fail LOUD, not silent: the image-on-S3 invariant cannot run, so new variants are NOT
        # verified to have an image. The operator must confirm {Key}.png exists before importing.
        log.warning("S3 unreachable: image-on-S3 invariant NOT enforced; verify new-variant images "
                    "exist before import", extra={"extra_fields": {"unverified_new_variants": len(new_keys)}})
    missing = {k for k in new_keys if not checker(k)} if checker is not None else set()
    drop = missing | delete_keys
    if not drop:
        return 0
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        p = to_upload / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        if not rows:
            continue                                   # truncated/empty -> skip, never crash the run
        header, body = rows[0], [r for r in rows[1:] if r and r[0] not in drop]
        csvio.atomic_write(p, lambda h, _rows=[header, *body]: csv.writer(h).writerows(_rows))
    if missing:
        log.warning("held new variants with no S3 image out of the upload (still queued)",
                    extra={"extra_fields": {"held": len(missing)}})
    if delete_keys:
        log.info("excluded to-delete variants from upload files",
                 extra={"extra_fields": {"excluded": len(delete_keys)}})
    return len(missing)


def prune_superseded_runs(outputs_root: Path) -> int:
    """Remove run folders that a newer run for the SAME source has superseded, so outputs/ keeps
    exactly one (current) snapshot per source and stale data can't accumulate. The latest run per
    source is always kept; the dedicated _inventory/ tree (no <source>_<date>_<time> shape at this
    level) is untouched. Returns the number of folders removed."""
    keep = {d.resolve() for d in latest_run_dirs(outputs_root)}
    removed = 0
    for d in Path(outputs_root).glob("*_*_*"):
        if d.is_dir() and d.resolve() not in keep:
            shutil.rmtree(d)
            removed += 1
    return removed


def collect_products(outputs_root: Path) -> dict[str, int]:
    """Gather each source's product import CSV into to_upload/: one file per source
    (3_products_<source>.csv) plus a combined all-sources file (3_products_all.csv),
    same Medusa import schema. So the products are next to the variants and tree."""
    to_upload = SETTINGS.paths.to_upload_dir
    to_upload.mkdir(parents=True, exist_ok=True)
    # prune stale per-source files first so a source that no longer runs cannot leave a
    # 3_products_<gone>.csv lingering in the upload set (3_products_all is rewritten below).
    for old in to_upload.glob("3_products_*.csv"):
        old.unlink()
    header: list[str] | None = None
    all_rows: list[list[str]] = []
    counts: dict[str, int] = {}
    for run_dir in latest_run_dirs(outputs_root):          # newest run per source only
        src_csv = run_dir / "4_products_import" / "medusa_import.csv"
        if not src_csv.exists():
            continue
        source = run_dir.name.rsplit("_", 2)[0]            # <source>_<date>_<time> -> <source>
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        if not rows:
            continue
        header_row, body = rows[0], rows[1:]
        # every source emits the SAME template header (emit.read_template_columns); refuse to merge
        # if one differs, or the combined 3_products_all.csv would silently mis-align columns.
        if header is None:
            header = header_row
        elif header_row != header:
            raise RuntimeError(
                f"collect_products: source '{source}' product header differs from earlier sources -- "
                "a column-schema mismatch would mis-align 3_products_all.csv; refusing to merge")
        counts[source] = len(body)
        # atomic (a half-written upload file would be read by verify_consistency right after); NOT
        # sanitized -- this is the Medusa IMPORT file, where a leading "'" would corrupt the data.
        csvio.write_rows(to_upload / f"3_products_{source}.csv", header_row, body)
        all_rows += body
    if header is not None:
        csvio.write_rows(to_upload / "3_products_all.csv", header, all_rows)
    return counts


def consolidate_inventory(inv_csvs: list[Path], to_upload: Path | None = None) -> int:
    """Merge the given per-run inventory_update.csv files into ONE deliverable:
    to_upload/<env>/4_inventory_update.csv. Deduped by Variant Sku (globally unique), last run
    wins -- so re-running against the same Medusa baseline never double-lists a SKU. Medusa's
    INVENTORY importer loads it WITHOUT a product re-import or any image work. Always written
    (header-only when nothing changed) so the deliverable is predictable. Returns SKU count."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    to_upload.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for src_csv in inv_csvs:
        if not src_csv.exists():
            continue
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:          # skip header
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r   # Variant Sku is column 0
    # atomic, NOT sanitized -- Medusa import file.
    csvio.write_rows(to_upload / "4_inventory_update.csv", emit.INVENTORY_COLUMNS, list(by_sku.values()))
    return len(by_sku)


def collect_inventory(outputs_root: Path) -> int:
    """Catalog path: consolidate the latest run per source's inventory delta."""
    return consolidate_inventory(
        [d / "4_products_import" / "inventory_update.csv" for d in latest_run_dirs(outputs_root)])


def collect_discontinued(outputs_root: Path) -> int:
    """Consolidate the latest run per source's delete-loop report into one review file:
    review/<env>/products_discontinued.csv (deduped by SKU). These are products the suppliers no
    longer carry -- already set to stock 0 by the inventory update, listed here for optional delete.
    Always written (header-only when none) so the file is a predictable, auditable deliverable."""
    review = SETTINGS.paths.review_dir
    review.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for d in latest_run_dirs(outputs_root):
        p = d / "review" / "products_discontinued.csv"
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r
    # operator-opened review file built from scraped product rows -> sanitize each cell + atomic.
    csvio.write_rows(review / "products_discontinued.csv", emit.DISCONTINUED_COLUMNS,
                     [[csvio.safe_cell(c) for c in r] for r in by_sku.values()])
    return len(by_sku)


def write_variants_to_delete() -> int:
    """Surface JUNK existing variants to review/<env>/variants_to_delete.csv for deletion from Medusa:
    BARE-CODE ones ('Mgt','Gs',... supplier codes wrongly minted) and MIS-TYPED ones ('Azul White
    Quartzite' keyed under type Onyx). Both are excluded from matching (loaders), so their products
    re-gap and (for mis-typed) re-mint under the correct type; deleting them in Medusa + re-exporting
    removes them everywhere. Their <env>/variations/{Key}.png on S3 should be deleted too."""
    exp = SETTINGS.paths.export_file
    if not exp.exists():
        return 0
    rows = []
    export_keys: set[str] = set()
    by_key: dict[str, dict] = {}
    with exp.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            name = (r.get("Name") or "").strip()
            key = (r.get("Key") or "").strip()
            if key:
                export_keys.add(key)
                by_key[key] = r
            reason = ""
            if name and looks_code_shaped(name) == "bare_code":
                reason = "bare_code: supplier code/brand abbreviation, not a variety"
            elif name and loaders.is_mistyped_variant(key, name):
                kt = key.split("_")[1] if key.count("_") >= 2 else "?"
                reason = f"mistyped: name says '{explicit_type_word(name)}' but keyed as type '{kt}'"
            if reason:
                rows.append({"Key": key, "Name": name, "Id": (r.get("Id") or "").strip(),
                             "Image": (r.get("Image") or "").strip(), "reason": reason})
    out = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    # Preserve a previously-flagged variant that auto-detection can't re-derive (e.g. a single-token
    # name like 'Sodalita' that isn't an explicit type word) AS LONG AS it is still in the export.
    # Once it is actually deleted in Medusa (gone from the export) it drops off here automatically.
    auto_keys = {r["Key"] for r in rows}
    if out.exists():
        for prev in csv.DictReader(out.open(encoding="utf-8-sig")):
            pk = (prev.get("Key") or "").strip()
            if pk and pk in export_keys and pk not in auto_keys:
                src = by_key.get(pk, {})
                rows.append({"Key": pk, "Name": (src.get("Name") or prev.get("Name") or "").strip(),
                             "Id": (src.get("Id") or prev.get("Id") or "").strip(),
                             "Image": (src.get("Image") or prev.get("Image") or "").strip(),
                             "reason": prev.get("reason") or "flagged for deletion"})
    out.parent.mkdir(parents=True, exist_ok=True)
    # operator-opened delete list carrying scraped Names -> sanitize against formula injection + atomic.
    csvio.write_dicts(out, ["Key", "Name", "Id", "Image", "reason"],
                      sorted(rows, key=lambda r: r["Key"]), sanitize=True)
    if rows:
        log.warning("junk variants in the export -- delete in Medusa + S3",
                    extra={"extra_fields": {"count": len(rows), "file": str(out),
                                            "names": sorted({r["Name"] for r in rows})}})
    return len(rows)


def _consistency_errors(export_ids: set[str], combo_ids: set[str], prod_ids: set[str],
                        inv_skus: set[str], known_skus: set[str],
                        prod_tuples: set[tuple[str, ...]] = frozenset(),
                        combo_tuples: set[tuple[str, ...]] = frozenset(),
                        ) -> tuple[list[str], list[str]]:
    """Pure set-arithmetic core of the consistency gate (testable without files). Every product and
    combination variation id must exist in the export; every inventory SKU must exist in Medusa; and
    every product's FULL (type,variation,finish,colour,quality) tuple must be an allowed combination."""
    if not export_ids:
        return (["variants_export.csv is missing or has no Ids -- cannot verify"], [])
    errors: list[str] = []
    warnings: list[str] = []
    if combo_ids - export_ids:
        errors.append(f"{len(combo_ids - export_ids)} combination variation ids are NOT in the current "
                      "export (stale combinations -- rebuild against the refreshed export)")
    if prod_ids - export_ids:
        errors.append(f"{len(prod_ids - export_ids)} product variation ids are NOT in the current export "
                      "(stale products -- rerun run all + catalog)")
    if prod_ids - combo_ids:
        # HARD: a product whose variation has no valid-combination row ships UNPRICEABLE in Medusa.
        # With the finish->Raw fallback every typed variation gets a combination, so this firing means
        # a genuinely uncovered variation (no resolvable type) is being emitted -- it must be assigned
        # a type (review/tree_uncovered_variations.csv) or held, not silently shipped.
        errors.append(f"{len(prod_ids - combo_ids)} product variations have NO valid-combination row "
                      "(unpriceable -- assign a type in tree_uncovered_variations.csv or hold them)")
    # Beyond variation-level coverage: the product's FULL (type,variation,finish,colour,quality) tuple
    # must itself be an allowed combination. The finish->Raw fallback guarantees every typed variation
    # gets SOME combination row, so the variation-level check above passes even when a product ships a
    # type/colour the tree never allowed for that variation (e.g. a product served on Quartzite while
    # the allowed set has the variety only under Marble). That disagreement ships UNPRICEABLE and is the
    # exact "backbone gap" that drafts the product downstream -- catch it here, structurally.
    if combo_tuples:
        orphan = prod_tuples - combo_tuples
        if orphan:
            errors.append(f"{len(orphan)} product combinations (type/colour/finish/quality) are NOT in "
                          "valid_combinations (the product feed disagrees with the allowed set -- the "
                          "product's type/colour was never allowed for its variation; fix the source "
                          "type/colour or add the combination)")
    # Inventory updates may ONLY target products that exist in Medusa -- never push stock for a product
    # that was never imported (an imageless product held out of the catalog, or a stray SKU). Only
    # checked once a Medusa product export is present (known_skus non-empty).
    if known_skus and (inv_skus - known_skus):
        errors.append(f"{len(inv_skus - known_skus)} inventory SKUs are NOT in the Medusa product export "
                      "(refusing to update stock for products that don't exist)")
    return (errors, warnings)


def verify_consistency() -> tuple[list[str], list[str]]:
    """Deterministic cross-artifact consistency gate -- no heuristics, no sampling. Every product and
    every valid-combination row must reference a variation id that EXISTS in the current export; every
    inventory SKU must exist in Medusa; every product's variation should have a valid combination. This
    catches stale or out-of-order artifacts (the classic failure: combinations not rebuilt after the
    export changed) before they ship, so correctness is structural, not something a human/AI eyeballs.
    Returns (errors, warnings); a non-empty errors list means the upload set is inconsistent."""
    p = SETTINGS.paths

    def _ids(path: Path, col: str) -> set[str]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as h:
                return {(r.get(col) or "").strip() for r in csv.DictReader(h) if (r.get(col) or "").strip()}
        except FileNotFoundError:
            return set()

    # Full-combination tuples, in ONE canonical field order (type, variation, finish, colour, quality),
    # so the product feed's STN ids and the valid-combinations columns compare directly. Category is
    # pinned by variation_id (Key prefix), so it is redundant in the tuple. Only complete tuples (every
    # component present) are compared -- an incomplete row is a missing-id case other gates already cover.
    def _tuples(path: Path, cols: tuple[str, ...]) -> set[tuple[str, ...]]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as h:
                reader = csv.DictReader(h)
                # Fail loud on a renamed/dropped column: a silent all(vals)==False would drop every row
                # and no-op the tuple gate, masking a real mismatch. Only checked when the file has a
                # header (an empty file legitimately yields no tuples).
                if reader.fieldnames and (missing := [c for c in cols if c not in reader.fieldnames]):
                    raise ValueError(f"{path.name} is missing expected column(s) {missing} -- the tuple "
                                     "consistency gate cannot run against a changed schema")
                out: set[tuple[str, ...]] = set()
                for r in reader:
                    vals = tuple((r.get(c) or "").strip() for c in cols)
                    if all(vals):
                        out.add(vals)
                return out
        except FileNotFoundError:
            return set()

    combo_cols = ("type_id", "variation_id", "finish_id", "color_id", "quality_id")
    prod_cols = ("STN Type Id", "STN Variation Id", "STN Finish Id", "STN Color Id", "STN Quality Id")

    from stone_pipeline.stages.product_state import load_known_products
    known = load_known_products()
    errors, warnings = _consistency_errors(
        export_ids=_ids(p.variants_export_csv, "Id"),
        combo_ids=_ids(p.to_upload_dir / "2_valid_combinations.csv", "variation_id"),
        prod_ids=_ids(p.to_upload_dir / "3_products_all.csv", "STN Variation Id"),
        inv_skus={s.upper() for s in _ids(p.to_upload_dir / "4_inventory_update.csv", "Variant Sku")},
        known_skus=set(known.by_sku),
        prod_tuples=_tuples(p.to_upload_dir / "3_products_all.csv", prod_cols),
        combo_tuples=_tuples(p.to_upload_dir / "2_valid_combinations.csv", combo_cols),
    )
    # Variety-identity uniqueness: emit collapses every (branch,type,name) to one survivor, so the emitted
    # full set must have NO identity appearing under two Keys. A failure here means a duplicate variety
    # would ship -- fail the whole produce loud rather than let it through (the bug this gate closes).
    errors += _identity_dup_errors(p.to_upload_dir / "1_variants_full.csv")
    return errors, warnings


def _identity_dup_errors(full_path: Path) -> list[str]:
    """Every (branch, type, name) in 1_variants_full must resolve to exactly ONE Key (emit's dedup gate)."""
    import collections
    from stone_pipeline.reference.loaders import variety_identity
    try:
        with full_path.open(encoding="utf-8-sig", newline="") as h:
            groups = collections.defaultdict(list)
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    groups[variety_identity(r["Key"], r.get("Name") or "")].append(r["Key"])
    except FileNotFoundError:
        return []
    return [f"duplicate variety {ident} under {len(keys)} Keys: {keys}"
            for ident, keys in groups.items() if len(keys) > 1]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    outputs_root = Path(argv[0]) if argv else SETTINGS.paths.outputs_dir
    steps = run(outputs_root)
    print(f"upload checklist: {steps}")
    print(f"  variants:  {steps.parent / '1_variants_full.csv'}  (or 1_variants_update.csv)")
    print(f"  products:  {steps.parent / '3_products_all.csv'}")
    print(f"  inventory: {steps.parent / '4_inventory_update.csv'}  (existing products' stock only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
