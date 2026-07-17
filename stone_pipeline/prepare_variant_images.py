"""Prepare NEW variant images: generate -> de-background -> upload to <env>/variations/.

The single command for steps 2-4 of the variant-image flow (image_pipeline/IMAGE_FLOW.md). Steps 1 (the
produce writes the queue, prompts_to_generate.json) and 5 (a later produce stamps the S3 image onto the
variant and un-holds its products) are already wired into produce; this closes the manual middle in ONE
reliable, idempotent step -- and uploads to the CORRECT prefix (dev/variations/), which was the gap: the
generic upload_artifacts targets dev/scraper/to_upload/, not the variations/ home a variant's Image URL
resolves to.

    python -m stone_pipeline.prepare_variant_images     # generate + upload the new-variant images

Queue = the imageless, product-backed variants (image_prompts.build). A variant with no product, or one
whose {Key}.png is already on S3, is never listed -- so a re-run never regenerates (or re-charges) an image
that exists: the S3 object is its own marker. Needs the generator stack (fal_client + torch + FAL_KEY); on
the lean image (which omits them on purpose) the queue is still written and the run reports where to run it,
spending nothing. gate_on_images never ships an imageless product, so a partial run just leaves those held.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.stages import image_prompts

log = logfmt.get_logger("prepare_variant_images")

# --- config (single block; no argparse, no inline magic numbers) --------------
VARIATIONS_PREFIX = f"{ENV_SEGMENT}/variations/"   # where a variant's {Key}.png must land (its Image URL home)
# rb_images.py writes the background-removed {Key}.png here (cwd=image_pipeline, OUTPUT_DIR="./to_upload").
DEBG_OUTPUT_DIR = SETTINGS.paths.workspace_root / "image_pipeline" / "to_upload"
# The queue image_prompts.build writes / genetate_images.py reads (ONE file, image_pipeline/).
PROMPTS_LOCAL = SETTINGS.paths.workspace_root / "image_pipeline" / "prompts_to_generate.json"
# The produce PUBLISHES its queue here so an on-demand GPU task (which has no local export/products/
# backbone_additions to rebuild it) can pull the exact queue the produce built. Env-scoped.
PROMPTS_S3_KEY = f"{ENV_SEGMENT}/scraper/prompts_to_generate.json"
GEN_DEPS = ("fal_client", "torch")                 # the generator stack the actual run needs to import


def _s3_client():
    """A bounded-timeout S3 client, or None when boto3/creds are absent (CI/local) so callers no-op.
    Mirrors refresh_images._s3_client so the whole codebase reaches variations/ the same way."""
    s3 = SETTINGS.s3
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None
    try:
        cfg = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
        return boto3.Session(profile_name=s3.credentials_profile or None,
                             region_name=s3.region).client("s3", config=cfg)
    except Exception:
        return None


def _target_keys(prompts_path: Path) -> list[str]:
    """The variant Keys in the just-built queue (output_name IS the Key)."""
    items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    return [i["output_name"] for i in items if i.get("output_name")]


def publish_prompts(prompts_path: Path, client=None) -> bool:
    """Upload the produce's just-built queue to S3 so an on-demand GPU texture job can pull the EXACT set
    the produce built (a fresh :gpu task has no local export/products/backbone_additions to rebuild it).
    Called by the produce when it dispatches the GPU job. No-op (returns False) when S3 is unreachable or
    dry-run, or the queue file is absent."""
    client = client or _s3_client()
    if client is None or SETTINGS.s3.dry_run or not prompts_path.is_file():
        return False
    try:
        client.upload_file(str(prompts_path), SETTINGS.s3.bucket, PROMPTS_S3_KEY)
        return True
    except Exception as exc:
        log.error("could not publish the texture queue to S3", extra={"extra_fields": {"error": str(exc)}})
        return False


def _fetch_published_prompts(client=None) -> bool:
    """Download the produce's published queue from S3 into PROMPTS_LOCAL. Returns True if it landed. Used
    by the GPU task, which cannot rebuild the queue locally. Absent object / unreachable S3 -> False (the
    caller then falls back to building locally, the dev-box path)."""
    client = client or _s3_client()
    if client is None:
        return False
    try:
        PROMPTS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(SETTINGS.s3.bucket, PROMPTS_S3_KEY, str(PROMPTS_LOCAL))
        return True
    except Exception:
        return False   # no published queue (e.g. running on a box that rebuilds it locally)


def _resolve_queue() -> Path:
    """The queue to generate from. Prefer the produce's PUBLISHED queue (the on-demand GPU task can't
    rebuild it -- no local export/products); fall back to building locally (dev box / an image-capable
    produce that has the inputs). Either way returns the path genetate_images.py reads."""
    if _fetch_published_prompts():
        log.info("using the produce's published texture queue from S3", extra={"extra_fields": {
            "key": PROMPTS_S3_KEY}})
        return PROMPTS_LOCAL
    return image_prompts.build()


def _generator_blockers() -> list[str]:
    """What is missing to actually generate HERE: the import deps + FAL_KEY. Empty == ready to run."""
    blockers = [m for m in GEN_DEPS if importlib.util.find_spec(m) is None]
    if not os.environ.get("FAL_KEY"):
        blockers.append("FAL_KEY")
    return blockers


def upload_variant_images(keys: list[str], client=None) -> tuple[int, list[str]]:
    """Upload each generated {Key}.png from the de-background output dir to <env>/variations/{Key}.png --
    the CORRECT prefix the variant's Image URL resolves to. Returns (uploaded, missing_keys). A key whose
    png the generator did not produce is REPORTED (missing), never silently skipped: its product stays HELD
    and the caller surfaces it. No-op (returns 0, all keys missing) when S3 is unreachable or dry-run."""
    client = client or _s3_client()
    if client is None or SETTINGS.s3.dry_run:
        return 0, list(keys)
    uploaded, missing = 0, []
    for key in keys:
        png = DEBG_OUTPUT_DIR / f"{key}.png"
        if not png.is_file():
            missing.append(key)
            continue
        client.upload_file(str(png), SETTINGS.s3.bucket, f"{VARIATIONS_PREFIX}{key}.png")
        uploaded += 1
    return uploaded, missing


def run() -> int:
    """Resolve the queue (produce's published one on S3, else build locally), generate + de-background,
    upload each {Key}.png to <env>/variations/. Returns the number uploaded (0 when nothing is queued or
    the generator stack is absent)."""
    prompts_path = _resolve_queue()
    targets = _target_keys(prompts_path)
    if not targets:
        log.info("no new variant images to prepare (every product-backed variant already has one on S3)")
        print("Nothing to prepare: every product-backed variant already has an image.")
        return 0

    blockers = _generator_blockers()
    if blockers:
        # the lean :core omits the generator stack on purpose; the queue is written, so run this where the
        # stack is present (fal_client + torch + FAL_KEY). Nothing generated or uploaded, nothing spent.
        log.warning("generator stack absent; queue written but not generated", extra={"extra_fields": {
            "queued": len(targets), "missing": blockers}})
        print(f"{len(targets)} new variant image(s) queued, but the generator can't run here "
              f"(missing {blockers}). Run where the stack is present; nothing was spent.")
        return 0

    from stone_pipeline.catalog import _generate_queued_images
    failed = _generate_queued_images()             # FLUX.2 -> BEN2 -> image_pipeline/to_upload/{Key}.png
    uploaded, missing = upload_variant_images(targets)
    if failed or missing:
        # loud: some images did not generate/upload, so their products stay HELD until a clean re-run.
        # Safe (gate_on_images never ships an imageless product) but surfaced, never silent.
        log.error("some variant images were not produced", extra={"extra_fields": {
            "queued": len(targets), "uploaded": uploaded, "failed_scripts": failed, "missing_pngs": missing}})
    log.info("variant images prepared", extra={"extra_fields": {"queued": len(targets), "uploaded": uploaded}})
    print(f"Prepared + uploaded {uploaded}/{len(targets)} new variant image(s) to {VARIATIONS_PREFIX}")
    return uploaded


if __name__ == "__main__":
    raise SystemExit(0 if run() >= 0 else 1)
