"""Auto-texture trigger: after a produce QUEUES new-variant textures it cannot generate locally (the lean
:core has no fal_client/torch), submit ONE on-demand GPU Batch job to generate + upload them.

Part of the produce (Republish), NOT a cron: it fires only when a produce actually queued new textures, and
only when BLOKPORT_AUTO_TEXTURE is set. Fire-and-forget -- produce submits and returns; the images land in
dev/variations/ and the NEXT produce stamps them (the same one-cycle hold as auto-enhance, so a partial or
failed run just leaves those products HELD, never shipped imageless).

Reuses the gpu_enhance Batch queue + job definition (same :gpu image, same IAM) with a RUN_MODE override:
RUN_MODE=generate-textures -> run_pipeline.sh -> python -m stone_pipeline.prepare_variant_images (the
UNCHANGED generate -> de-bg -> upload-to-variations pipeline). The only new deploy requirement is `ben2` in
the :gpu image (rb_images imports it); nothing about generation/refresh/enhance changes.
"""

from __future__ import annotations

from stone_pipeline.config.settings import S3_REGION, SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("texture_trigger")


def submit_texture_job(queued: int) -> str | None:
    """Submit ONE GPU Batch job (RUN_MODE=generate-textures) to generate + upload the `queued` new-variant
    textures. Returns the job id, or None when disabled / misconfigured / nothing queued. A submit failure
    logs loud and returns None -- it NEVER raises, so it can never fail the produce (the textures stay queued
    for the next run). Idempotent downstream: prepare_variant_images skips images already on S3, so an
    overlapping re-submit re-charges nothing already produced."""
    cfg = SETTINGS.auto_texture
    if not cfg.enabled:
        log.info("auto-texture disabled (BLOKPORT_AUTO_TEXTURE unset); no GPU job submitted")
        return None
    if queued <= 0:
        return None
    if not cfg.queue or not cfg.job_definition:
        log.warning("auto-texture enabled but queue/job-definition not configured; skipping",
                    extra={"extra_fields": {"queue": cfg.queue, "job_definition": cfg.job_definition}})
        return None
    try:
        import boto3

        batch = boto3.client("batch", region_name=S3_REGION)
    except Exception as exc:  # no boto3/creds -> loud, but produce continues
        log.warning("auto-texture: boto3/clients unavailable; skipping",
                    extra={"extra_fields": {"error": str(exc)}})
        return None
    try:
        resp = batch.submit_job(
            jobName="autotexture",
            jobQueue=cfg.queue,
            jobDefinition=cfg.job_definition,
            # override the job-def's default RUN_MODE (reprocess) so the SAME :gpu container runs the texture
            # pipeline instead of the de-watermark/enhance one. No SRC/window env: one job does the whole queue.
            containerOverrides={"environment": [{"name": "RUN_MODE", "value": "generate-textures"}]})
        job_id = resp["jobId"]
        log.info("auto-texture submitted", extra={"extra_fields": {"queued": queued, "job_id": job_id}})
        return job_id
    except Exception as exc:  # a submit failure is loud but non-fatal to the produce
        log.error("auto-texture: submit_job failed",
                  extra={"extra_fields": {"queued": queued, "error": str(exc)}})
        return None
