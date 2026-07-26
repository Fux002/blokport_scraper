"""One-time QUALITY refresh of product-backed variant images.

Many early {Key}.png were made with an inferior model. This re-makes each product-backed variant's image
ONCE with the current best FLUX.2 model, overwriting the same dev/variations/{Key}.png in place (one image
per variant, replaced -- no new name, no orphan). Cost-gated and idempotent: a durable S3 marker records
every Key already refreshed, so a re-run never regenerates -- and re-charges -- the same image twice.

    python -m stone_pipeline.refresh_images            # DRY RUN: how many + estimated cost, nothing spent
    BLOKPORT_REFRESH_APPLY=1 python -m stone_pipeline.refresh_images   # generate + upload + mark
    BLOKPORT_REFRESH_BATCH=500 BLOKPORT_REFRESH_APPLY=1 ...            # cap this run to 500 (cost spread)

Drain the legacy backlog gradually by scheduling the --apply form with a BATCH cap: each run upgrades the
next batch and marks them, so over successive runs every legacy image lands on the best model exactly once.

Selection = product-backed AND already-imaged AND not-yet-refreshed. A variant with no product, or no image
yet (that is build()'s new-image queue), is never touched. Requires the generator stack (fal_client + torch
+ FAL_KEY) to actually run; without it the dry run still reports the plan. No em dashes (design principle 2).
"""

from __future__ import annotations

import importlib.util
import json
import os

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.stages import image_prompts

log = logfmt.get_logger("refresh_images")

# --- config (single block; no argparse, no inline magic numbers) --------------
BEST_MODEL = "flux-2-max"                       # the model the refresh re-makes with (genetate_images MODEL)
PER_IMAGE_USD = 0.100                            # FLUX.2 [max] approx per edit (genetate_images MODELS[max])
MARKER_KEY = f"{ENV_SEGMENT}/variations/_refreshed.json"   # durable "already refreshed" set, next to the images
APPLY_ENV = "BLOKPORT_REFRESH_APPLY"             # set to "1" to spend; unset = dry run
BATCH_ENV = "BLOKPORT_REFRESH_BATCH"             # max images to refresh THIS run (cost spread); 0/unset = all


def _batch_cap() -> int:
    """Per-run ceiling so a scheduled refresh drains the legacy backlog gradually (the marker makes each
    Key once-only). 0/unset = no cap (do every eligible image this run)."""
    raw = os.environ.get(BATCH_ENV, "").strip()
    return int(raw) if raw.isdigit() else 0
_GEN_DEPS = ("fal_client", "torch")             # the generator stack the actual run needs


def _s3_client():
    """A bounded-timeout S3 client, or None when boto3/creds are absent (CI/local) so callers no-op.
    Mirrors emit_catalog._s3_variation_keys so the whole codebase reaches variations/ the same way."""
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


def parse_marker(blob: bytes | str) -> set[str]:
    """The refreshed-Key set from the marker's JSON bytes. Tolerant of an absent/blank/corrupt marker
    (returns empty) so a first run, not a crash, is the failure mode. Pure -- unit-testable without S3."""
    try:
        data = json.loads(blob) if blob else []
        return {str(k) for k in data} if isinstance(data, list) else set()
    except (ValueError, TypeError):
        log.warning("refreshed marker unparseable; treating as empty (all product-backed images eligible)")
        return set()


def load_refreshed(client=None) -> set[str]:
    """The set of Keys already refreshed with the best model, from the S3 marker. Empty when S3 is
    unreachable OR the marker does not exist yet (a first run)."""
    client = client or _s3_client()
    if client is None:
        return set()
    try:
        obj = client.get_object(Bucket=SETTINGS.s3.bucket, Key=MARKER_KEY)
        return parse_marker(obj["Body"].read())
    except Exception:
        return set()   # no marker yet (first run) or unreachable: nothing is known-refreshed


def save_refreshed(keys: set[str], client=None) -> bool:
    """Persist the refreshed-Key set to the S3 marker. Returns False (no-op) when S3 is unreachable or a
    dry-run bucket is configured, so a marker is only written when images were actually uploaded."""
    client = client or _s3_client()
    if client is None or SETTINGS.s3.dry_run:
        return False
    try:
        client.put_object(Bucket=SETTINGS.s3.bucket, Key=MARKER_KEY,
                          Body=json.dumps(sorted(keys)).encode("utf-8"), ContentType="application/json")
        return True
    except Exception as exc:
        log.error("could not persist refreshed marker", extra={"extra_fields": {"error": str(exc)}})
        return False


def _target_keys(prompts_path) -> list[str]:
    """The variant Keys in a just-built refresh queue (output_name IS the Key)."""
    items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    return [i["output_name"] for i in items if i.get("output_name")]


def plan(refreshed: set[str]) -> tuple[list[str], float]:
    """Build the refresh queue for every product-backed, imaged, not-yet-refreshed variant and return
    (target Keys, estimated cost USD). Writes prompts_to_generate.json as a side effect (the generator's
    single input), so an --apply run generates exactly what the plan reported."""
    path = image_prompts.build_refresh(refreshed)
    targets = _target_keys(path)
    return targets, round(len(targets) * PER_IMAGE_USD, 2)


def run() -> int:
    """Dry run by default (report count + cost, spend nothing); BLOKPORT_REFRESH_APPLY=1 generates,
    uploads, and marks. Returns the number of images refreshed (0 on a dry run)."""
    refreshed = load_refreshed()
    targets, _cost = plan(refreshed)
    # Cost spread: cap THIS run to a batch; the rest stay eligible and drain on later (scheduled) runs.
    cap = _batch_cap()
    batch = targets[:cap] if cap and cap < len(targets) else targets
    cost = round(len(batch) * PER_IMAGE_USD, 2)
    apply = os.environ.get(APPLY_ENV) == "1"
    log.info("image refresh plan", extra={"extra_fields": {
        "eligible": len(targets), "this_run": len(batch), "already_refreshed": len(refreshed),
        "model": BEST_MODEL, "est_cost_usd": cost, "apply": apply}})
    if not targets:
        print("Nothing to refresh: every product-backed image is already on the best model.")
        return 0
    remaining = len(targets) - len(batch)
    print(f"{len(targets)} eligible; this run refreshes {len(batch)} with {BEST_MODEL}, est. ~${cost:.2f}"
          + (f" ({remaining} left for later runs)." if remaining else "."))
    if not apply:
        print(f"DRY RUN. Set {APPLY_ENV}=1 to generate + upload + mark. Nothing was spent.")
        return 0
    missing = [m for m in _GEN_DEPS if importlib.util.find_spec(m) is None]
    if missing or not os.environ.get("FAL_KEY"):
        # the deployed :core lacks the generator stack on purpose; the queue is written, so run the
        # generator on a box that HAS it (fal_client + torch + FAL_KEY), then re-run --apply to mark.
        print(f"Queue written, but the generator can't run here (missing {missing or 'FAL_KEY'}). "
              f"Run image_pipeline where the stack is present; nothing was marked.")
        return 0
    if len(batch) < len(targets):
        image_prompts.build_for_keys(batch)        # narrow the generator queue to just this run's batch
    from stone_pipeline.catalog import _generate_queued_images
    failed = _generate_queued_images()             # FLUX.2 max -> BEN2 -> local {Key}.png (reused runner)
    from deploy import upload_artifacts
    upload_artifacts.main()                        # local {Key}.png -> dev/variations/ (reused uploader)
    if failed:
        # some scripts failed: mark nothing this run so the failed Keys stay eligible and re-cost nothing
        # already spent. A clean re-run picks them up.
        log.error("refresh generation had failures; marker NOT advanced",
                  extra={"extra_fields": {"failed": failed, "eligible": len(batch)}})
        return 0
    save_refreshed(refreshed | set(batch))         # mark only this run's batch as done
    print(f"Refreshed + marked {len(batch)} image(s)." + (f" {remaining} remain for later runs." if remaining else ""))
    return len(batch)


if __name__ == "__main__":
    raise SystemExit(0 if run() >= 0 else 1)
