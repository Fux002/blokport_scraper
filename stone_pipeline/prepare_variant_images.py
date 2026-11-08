"""The variant textures, one path (image_pipeline/IMAGE_FLOW.md).

The produce (catalog.run -> queue_new_textures) builds the queue (the imageless, product-backed variants, plus the
capped upgrade drip), publishes it to a unique S3 key and dispatches ONE on-demand GPU job bound to it
(stone_pipeline.io.texture_job, RUN_MODE=generate-textures). The produce never generates. The job, or a dev box, runs:

    python -m stone_pipeline.prepare_variant_images     # generate + de-background + upload to <env>/variations/

A variant with no product, or one whose {Key}.png is already on S3, is never generated, so a re-run never
regenerates (or re-charges) an image that exists: the S3 object is its own marker. Generating needs the stack
(fal_client + torch + FAL_KEY); without it the run spends nothing and fails (TextureJobFailed, a non-zero exit, so
the failed-job alert fires). The sync's product gate never ships an imageless PRODUCT (a product lists only once its
{Key}.png is on S3), so a partial run just leaves those products held; the variant RECORDS stay in Medusa.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from stone_pipeline import refresh_images
from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.io import storage
from stone_pipeline.stages import image_prompts

log = logfmt.get_logger("prepare_variant_images")


# --- config (single block; no argparse, no inline magic numbers) --------------
VARIATIONS_PREFIX = f"{ENV_SEGMENT}/variations/"   # where a variant's {Key}.png must land (its Image URL home)
# rb_images.py writes the background-removed {Key}.png here (cwd=image_pipeline, OUTPUT_DIR="./to_upload").
DEBG_OUTPUT_DIR = SETTINGS.paths.workspace_root / "image_pipeline" / "to_upload"
# genetate_images.py writes FLUX's {Key}.png here (cwd=image_pipeline, OUTPUT_DIR="./images") and resumes over what is there.
GEN_OUTPUT_DIR = SETTINGS.paths.workspace_root / "image_pipeline" / "images"
# The queue image_prompts.build writes / genetate_images.py reads (ONE file, image_pipeline/).
PROMPTS_LOCAL = SETTINGS.paths.workspace_root / "image_pipeline" / "prompts_to_generate.json"
# The produce PUBLISHES its queue under a UNIQUE key here and passes that key to the GPU job via
# TEXTURE_QUEUE_KEY_ENV: the on-demand GPU task has no local export/products/backbone_additions to rebuild the
# queue, and overlapping mints never share (and clobber) one queue object. Env-scoped.
TEXTURE_QUEUE_PREFIX = f"{ENV_SEGMENT}/scraper/texture_queues/"
TEXTURE_QUEUE_KEY_ENV = "BLOKPORT_TEXTURE_QUEUE_KEY"   # the GPU job reads its exact queue key from here
BATCH_JOB_ENV = "AWS_BATCH_JOB_ID"   # set by Batch in every job it runs: this process is the dispatched GPU task
GEN_DEPS = ("fal_client", "torch")                 # the generator stack the actual run needs to import
GENERATOR_SCRIPTS = ("genetate_images.py", "rb_images.py")   # FLUX.2 -> BEN2, run in this order
# The job's own exit codes, apart from a crash (1) and run_pipeline.sh's bad mode (2). Both fail the Batch job (the
# failed-job alert fires once); the job definition never retries them (evaluateOnExit), so FAL is never paid twice.
EXIT_CANNOT_RUN = 3   # the job could not do its queue: no generator here, or its dispatched queue is unreadable
EXIT_PARTIAL = 4      # it ran, but some textures did not land (they stay unmarked, so the next produce re-queues them)


class TextureJobError(RuntimeError):
    """The texture job did not finish its queue; main() turns it into the job's exit code, so the failed-job
    alert fires instead of a quiet exit 0 with products HELD. Raised as one of its two kinds below; one raised
    unclassified exits as a job that could not run."""
    exit_code: int = EXIT_CANNOT_RUN


class TextureJobFailed(TextureJobError):
    """The job could not do its queue: no generator here, or its dispatched queue is unreadable."""
    exit_code = EXIT_CANNOT_RUN


class TexturesIncomplete(TextureJobError):
    """The job ran, but some textures did not land (a script failed, a PNG is missing or its upload failed), or
    the best-model marker was not written."""
    exit_code = EXIT_PARTIAL


def _s3_client():
    """A bounded-timeout S3 client, or None when boto3/creds are absent (CI/local) so callers no-op.
    Mirrors refresh_images._s3_client so the whole codebase reaches variations/ the same way."""
    s3 = SETTINGS.s3
    try:
        return storage.aws_client("s3", config=storage.BOUNDED, profile=s3.credentials_profile)
    except Exception:
        return None


def _target_keys(prompts_path: Path) -> list[str]:
    """The variant Keys in the queue (output_name IS the Key). A queue that cannot be read, or is not a list of
    objects, is one the job cannot do (TextureJobFailed): never a crash, and never read as an empty queue."""
    try:
        items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    except (OSError, ValueError) as exc:          # ValueError: JSONDecodeError and UnicodeDecodeError
        raise TextureJobFailed(f"the texture queue {prompts_path} cannot be read: {exc}") from exc
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise TextureJobFailed(f"the texture queue {prompts_path} is not a list of objects")
    return [i["output_name"] for i in items if i.get("output_name")]


def publish_prompts(prompts_path: Path, key: str, client=None) -> bool:
    """Upload the produce's just-built queue to S3 so an on-demand GPU texture job can pull the EXACT set
    the produce built (a fresh :gpu task has no local export/products/backbone_additions to rebuild it).
    Called by the produce when it dispatches the GPU job, with a UNIQUE per-dispatch key so a concurrent
    produce can't clobber it. No-op (returns False) when S3 is unreachable or dry-run, or the queue file is
    absent -- the caller MUST treat False as 'do not dispatch a GPU job against a stale queue'."""
    client = client or _s3_client()
    if client is None or SETTINGS.s3.dry_run or not prompts_path.is_file():
        return False
    try:
        client.upload_file(str(prompts_path), SETTINGS.s3.bucket, key)
    except Exception as exc:
        log.error("could not publish the texture queue to S3", extra={"extra_fields": {"error": str(exc)}})
        return False
    _prune_stale_queue_keys(client)   # self-heal: drop old per-dispatch queues so the prefix never grows unbounded
    return True


def _prune_stale_queue_keys(client) -> int:
    """Delete per-dispatch queue objects under TEXTURE_QUEUE_PREFIX older than SETTINGS.auto_texture.queue_ttl_seconds --
    they are ephemeral (each is consumed by its GPU job within minutes), so without this the prefix grows
    forever (one object per mint-batch, including failed dispatches). Best-effort: a cleanup failure NEVER
    fails the produce (the images are what matter); it only logs. Returns the count deleted. The TTL is far
    above any job's runtime, so a running or just-published queue is never touched."""
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - SETTINGS.auto_texture.queue_ttl_seconds
    deleted = 0
    try:
        for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=SETTINGS.s3.bucket, Prefix=TEXTURE_QUEUE_PREFIX):
            stale = [{"Key": o["Key"]} for o in page.get("Contents", [])
                     if o["LastModified"].timestamp() < cutoff]
            if stale:
                client.delete_objects(Bucket=SETTINGS.s3.bucket, Delete={"Objects": stale, "Quiet": True})
                deleted += len(stale)
    except Exception as exc:
        log.warning("stale texture-queue cleanup skipped (non-fatal)",
                    extra={"extra_fields": {"error": str(exc)}})
        return deleted
    if deleted:
        log.info("pruned stale texture queues", extra={"extra_fields": {"deleted": deleted}})
    return deleted


def _fetch_published_prompts(key: str, client=None) -> bool:
    """Download the dispatched queue `key` from S3 into PROMPTS_LOCAL. True if it landed; False when S3 is
    unreachable, the object cannot be read, or it cannot be written here (the caller fails the job loud)."""
    client = client or _s3_client()
    if client is None:
        return False
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        PROMPTS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(SETTINGS.s3.bucket, key, str(PROMPTS_LOCAL))
    except (BotoCoreError, ClientError, OSError) as exc:   # OSError: the local directory or file write
        log.error("could not read the dispatched texture queue",
                  extra={"extra_fields": {"key": key, "error": str(exc)}})
        return False
    return True


def _prune_already_on_s3(prompts_path: Path, targets: list[str]) -> list[str]:
    """Drop queue entries whose {Key}.png is ALREADY in <env>/variations/ on S3 and rewrite prompts_path so
    the generator itself skips them -- the S3 object is the idempotency marker, so a re-dispatch never
    regenerates (or re-charges FAL for) an image that exists. S3 unreachable (keys is None) -> no pruning and
    generate all: the safe default, since we cannot prove an image exists (better a rare re-charge than
    skipping a genuinely missing image and shipping a variant imageless)."""
    from stone_pipeline.stages.emit_catalog import _s3_variation_keys
    existing = _s3_variation_keys()
    if not existing:
        return targets
    items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    # An UPGRADE item's {Key}.png exists BY DEFINITION (that is what makes it an upgrade rather than a
    # mint), so the mint-idempotency rule below would drop every one of them and silently generate
    # nothing. Keep upgrades; prune only mints, which is where the re-charge risk actually is.
    from stone_pipeline.stages.image_prompts import UPGRADE_MODE
    upgrades = {i.get("output_name") for i in items if i.get("mode") == UPGRADE_MODE}
    keep = [i for i in items
            if i.get("output_name") not in existing or i.get("output_name") in upgrades]
    dropped = len(items) - len(keep)
    if dropped:
        prompts_path.write_text(json.dumps(keep), encoding="utf-8")
        log.info("skipped variant images already on S3 (idempotent re-dispatch)",
                 extra={"extra_fields": {"skipped": dropped}})
    return [k for k in targets if k not in existing or k in upgrades]


def _resolve_queue() -> Path:
    """The queue to generate from. A dispatched job (TEXTURE_QUEUE_KEY_ENV set) uses exactly the queue its
    produce published: it cannot rebuild one (no local export/products), so an unreadable queue fails the job,
    and so does a Batch job given no key. A dev box (no key, not in Batch) builds the queue locally. Either way
    returns the path genetate_images.py reads."""
    key = os.environ.get(TEXTURE_QUEUE_KEY_ENV)
    if not key:
        if os.environ.get(BATCH_JOB_ENV):
            raise TextureJobFailed(f"a Batch texture job was started without {TEXTURE_QUEUE_KEY_ENV}; it cannot "
                                   "rebuild the queue here (no local export)")
        return image_prompts.build()
    if not _fetch_published_prompts(key):
        raise TextureJobFailed(f"the dispatched texture queue {key} could not be read from S3")
    log.info("using the produce's published texture queue from S3", extra={"extra_fields": {"key": key}})
    return PROMPTS_LOCAL


def _generator_blockers() -> list[str]:
    """What is missing to actually generate HERE: the import deps + FAL_KEY. Empty == ready to run."""
    blockers = [m for m in GEN_DEPS if importlib.util.find_spec(m) is None]
    if not os.environ.get("FAL_KEY"):
        blockers.append("FAL_KEY")
    return blockers


def _find_png(key: str) -> Path | None:
    """The finished {key}.png: BEN2's output in DEBG_OUTPUT_DIR only (the scripts run with cwd=image_pipeline,
    so that location is fixed). Never anywhere else: FLUX's images/{key}.png still has its background, and
    uploading it would mark a wrong texture final. Absent -> None (the key is missing, and loud)."""
    png = DEBG_OUTPUT_DIR / f"{key}.png"
    return png if png.is_file() else None


def upload_variant_images(keys: list[str], client=None) -> tuple[int, list[str]]:
    """Upload each generated {Key}.png (found under image_pipeline/) to <env>/variations/{Key}.png -- the
    CORRECT prefix the variant's Image URL resolves to. Returns (uploaded, missing_keys). A key whose png the
    generator did not produce, or whose upload failed, is REPORTED (missing), never silently skipped: its product
    stays HELD and the caller surfaces it. No-op (returns 0, all keys missing) when S3 is unreachable or dry-run."""
    client = client or _s3_client()
    if client is None or SETTINGS.s3.dry_run:
        return 0, list(keys)
    from boto3.exceptions import S3UploadFailedError
    from botocore.exceptions import BotoCoreError, ClientError
    uploaded, missing = 0, []
    for key in keys:
        png = _find_png(key)
        if png is None:
            missing.append(key)
            continue
        try:
            client.upload_file(str(png), SETTINGS.s3.bucket, f"{VARIATIONS_PREFIX}{key}.png")
        except (S3UploadFailedError, ClientError, BotoCoreError) as exc:
            # this key's miss, not the job's crash: the rest still land and are marked (never paid twice)
            log.error("variant texture upload failed", extra={"extra_fields": {"key": key, "error": str(exc)}})
            missing.append(key)
            continue
        uploaded += 1
    return uploaded, missing


def queue_new_textures() -> int:
    """The produce's texture step: build the queue (the new product-backed variants, plus up to images.upgrade_batch
    older textures re-made, read against the best-model marker), then, with AUTO_TEXTURE on, publish it to a UNIQUE
    key and dispatch ONE GPU texture job bound to exactly that key (a concurrent produce can never clobber a queue a
    starting job is about to pull). The produce never generates: the job does (run). Their products stay HELD until
    {Key}.png exists, so every way the job cannot be dispatched is loud. Returns the number of queued textures."""
    # The upgrade drip: once-only via the durable best-model marker, capped so a produce's FAL spend stays
    # predictable. A legacy texture is never held back, just replaced within a few produces. cap 0 == mints only.
    cap = SETTINGS.images.upgrade_batch
    try:
        refreshed = refresh_images.load_refreshed() if cap > 0 else set()
    except refresh_images.MarkerUnreadable:
        # read as empty, every texture already made by the best model would be queued (and paid) again
        log.exception("best-model marker unreadable: this produce queues mints only, no upgrades")
        cap, refreshed = 0, set()
    try:
        prompts_path = image_prompts.build_with_upgrades(refreshed, cap) if cap > 0 else image_prompts.build()
    except (OSError, ValueError, csv.Error):     # an input that cannot be read; a bug in the build fails the produce
        log.exception("image prompt queue build failed")
        return 0
    items = json.loads(prompts_path.read_text(encoding="utf-8"))   # the build always writes a list
    if not items:
        return 0
    # The produce's own FAL_KEY and torch do not matter here: the GPU job carries its own (_generator_blockers).
    # Loud when nothing will generate them: at cold-start scale an unserved product lane must not read as clean.
    if SETTINGS.auto_texture.enabled:
        _dispatch(prompts_path, len(items))
    else:
        log.warning("%d new variant texture(s) queued but AUTO_TEXTURE is off, so no GPU job generates them; "
                    "their products stay HELD until `python -m stone_pipeline.prepare_variant_images` runs",
                    len(items), extra={"extra_fields": {"queued": len(items)}})
    return len(items)


def _dispatch(prompts_path: Path, queued: int) -> None:
    """Publish this produce's queue to a unique key and submit the GPU texture job against it; only after a
    successful publish (a no-op publish, S3 dry-run or unreachable, would leave the job a stale or absent queue).
    Never fails the produce: the textures stay queued for the next run, and the failure is loud."""
    from stone_pipeline.io.texture_job import submit_texture_job
    dispatched = False
    try:
        queue_key = f"{TEXTURE_QUEUE_PREFIX}{uuid.uuid4().hex}.json"
        if publish_prompts(prompts_path, queue_key):
            dispatched = bool(submit_texture_job(queued, queue_key=queue_key))
            if dispatched:
                log.info("%d new variant texture(s) queued for the GPU texture job; their products stay HELD "
                         "until it lands", queued, extra={"extra_fields": {"queued": queued, "queue_key": queue_key}})
        else:
            log.error("texture queue publish no-op'd (S3 dry-run/unreachable); NOT dispatching a GPU job "
                      "against a stale queue", extra={"extra_fields": {"queued": queued}})
    except Exception:
        log.exception("auto-texture trigger failed (non-fatal; textures stay queued for the next run)")
    if not dispatched:
        log.error("%d new variant texture(s) queued but NO GPU job was dispatched; their products stay "
                  "HELD until a successful produce publishes + dispatches (check S3 write / "
                  "GPU queue+jobdef config)", queued, extra={"extra_fields": {"queued": queued}})


def _clear_local_outputs(keys: list[str]) -> None:
    """Remove the queued keys' local FLUX and BEN2 outputs before generating. genetate_images resumes over an existing
    images/{Key}.png and BEN2 de-backgrounds it, so a PNG an earlier run left on a dev box would otherwise be uploaded
    as this run's texture. A fresh GPU task has none. What is already on S3 was pruned from `keys` before this."""
    for key in keys:
        for folder in (GEN_OUTPUT_DIR, DEBG_OUTPUT_DIR):
            (folder / f"{key}.png").unlink(missing_ok=True)


def _generate_queued_images() -> list[str]:
    """Run the image_pipeline chain (FLUX.2 -> BEN2) on the queued prompts, in this process's interpreter: the
    committed scripts stay the one generator. Returns the scripts that exited non-zero or timed out (empty: all
    succeeded)."""
    ip = SETTINGS.paths.workspace_root / "image_pipeline"
    failed: list[str] = []
    for script in GENERATOR_SCRIPTS:
        if (ip / script).exists():
            # sys.executable so the venv interpreter is used; -u so each image's progress streams to CloudWatch
            # live (a block-buffered BEN2 stage looks like a silent hang); the timeout so a wedged stage can never
            # pin the Batch task forever (a timeout is a failed script)
            try:
                rc = subprocess.run([sys.executable, "-u", script], cwd=ip, check=False,
                                    timeout=SETTINGS.auto_texture.script_timeout_s).returncode
            except subprocess.TimeoutExpired:
                log.error("image-pipeline script timed out",
                          extra={"extra_fields": {"script": script, "timeout_s": SETTINGS.auto_texture.script_timeout_s}})
                failed.append(script)
                continue
            if rc != 0:
                log.error("image-pipeline script failed", extra={"extra_fields": {"script": script, "returncode": rc}})
                failed.append(script)
    return failed


def run() -> int:
    """Resolve the queue (the dispatched one on S3, else build locally), generate + de-background, upload
    each {Key}.png to <env>/variations/. Returns the number uploaded (0 when nothing is queued). Raises
    TextureJobFailed when the queue cannot be read or the generator cannot run here, and TexturesIncomplete
    when some textures did not land."""
    prompts_path = _resolve_queue()
    targets = _target_keys(prompts_path)
    if not targets:
        log.info("no new variant images to prepare (every product-backed variant already has one on S3)")
        print("Nothing to prepare: every product-backed variant already has an image.")
        return 0

    # Idempotency: prune queue entries whose image is already on S3 BEFORE generating, so a re-dispatch
    # (overlapping mints, a re-Republish) never re-charges FAL for an image that exists.
    targets = _prune_already_on_s3(prompts_path, targets)
    if not targets:
        log.info("every queued variant image is already on S3; nothing to generate")
        print("Nothing to prepare: every queued variant image already exists on S3.")
        return 0

    blockers = _generator_blockers()
    if blockers:
        # nothing generated or uploaded, nothing spent; the products stay HELD, so the job must fail loud
        raise TextureJobFailed(f"{len(targets)} variant texture(s) queued, but the generator cannot run here: "
                               f"missing {blockers} (fal_client + torch + FAL_KEY)")

    _clear_local_outputs(targets)                  # never re-upload a PNG an earlier run left behind
    failed = _generate_queued_images()             # FLUX.2 -> BEN2 -> image_pipeline/to_upload/{Key}.png
    uploaded, missing = upload_variant_images(targets)
    not_landed = set(missing)
    landed = [k for k in targets if k not in not_landed]
    marked = True
    if landed:
        # Every texture that landed was made by the CURRENT best model, so record it as such, partial run or not:
        # otherwise the upgrade drip would later regenerate it (a mint looks legacy, an upgrade looks undone),
        # paying twice for an image that is already best. What did not land stays unmarked: the next produce
        # queues it again (a mint is still imageless; an upgrade is still outside the marker).
        marked = refresh_images.mark_best_model(landed)
    if failed or missing or not marked:
        # loud: some images did not generate/upload, so their products stay HELD until a clean re-run.
        # Safe (the sync product gate never lists an imageless product) but surfaced, never silent. Include where we
        # looked, so a path/output-dir mismatch on a runner is diagnosable from the log rather than guessed.
        def _peek(p: Path):
            try:
                return sorted(x.name for x in p.iterdir())[:6]
            except Exception as exc:
                return f"<{exc}>"
        ip_root = SETTINGS.paths.workspace_root / "image_pipeline"
        log.error("some variant images were not produced", extra={"extra_fields": {
            "queued": len(targets), "uploaded": uploaded, "failed_scripts": failed, "marker_written": marked,
            "missing_pngs": missing[:8], "debg_dir": str(DEBG_OUTPUT_DIR), "debg_files": _peek(DEBG_OUTPUT_DIR),
            "ip_to_upload": _peek(ip_root / "to_upload"), "ip_images": _peek(ip_root / "images"),
            "cwd": os.getcwd(), "workspace_root": str(SETTINGS.paths.workspace_root)}})
        raise TexturesIncomplete(f"{len(missing)} of {len(targets)} variant texture(s) did not land "
                                 f"(failed scripts {failed}); uploaded {uploaded}; best-model marker "
                                 f"{'written' if marked else 'NOT written'}")
    log.info("variant images prepared", extra={"extra_fields": {"queued": len(targets), "uploaded": uploaded}})
    print(f"Prepared + uploaded {uploaded}/{len(targets)} new variant image(s) to {VARIATIONS_PREFIX}")
    return uploaded


def main() -> int:
    """The job's entry: 0 when the queue is done, else the error's own exit code (EXIT_CANNOT_RUN /
    EXIT_PARTIAL), so the Batch job fails loud and its definition knows not to retry it."""
    try:
        run()
    except TextureJobError as exc:
        log.error("the texture job did not finish its queue",
                  extra={"extra_fields": {"error": str(exc), "exit_code": exc.exit_code}})
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
