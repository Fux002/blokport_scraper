"""Auto-texture trigger: after a produce QUEUES new-variant textures (the produce never generates them),
submit ONE on-demand GPU Batch job to generate + upload them.

Part of the produce (Republish), NOT a cron: it fires only when a produce actually queued new textures, and
only when BLOKPORT_AUTO_TEXTURE is set. Fire-and-forget -- produce submits and returns; the images land in
dev/variations/ and the NEXT produce stamps them (a one-cycle hold, so a partial or failed run just leaves
those products HELD, never shipped imageless).

Runs on the gpu_enhance Batch queue + job definition (the :gpu image), RUN_MODE=generate-textures ->
run_pipeline.sh -> python -m stone_pipeline.prepare_variant_images (generate -> de-bg -> upload-to-variations).
The :gpu image must carry `ben2` (rb_images imports it).
"""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client

log = logfmt.get_logger("texture_trigger")


def submit_texture_job(queued: int, queue_key: str) -> str | None:
    """Submit ONE GPU Batch job (RUN_MODE=generate-textures) to generate + upload the `queued` new-variant
    textures. `queue_key` is the UNIQUE per-dispatch S3 key of the queue this job must pull (passed as
    BLOKPORT_TEXTURE_QUEUE_KEY), so overlapping mints never share one clobberable queue object. Returns the
    job id, or None when disabled / misconfigured / nothing queued. A submit failure logs loud and returns
    None -- it NEVER raises, so it can never fail the produce (the textures stay queued for the next run).
    Idempotent downstream: prepare_variant_images prunes images already on S3, so an overlapping re-submit
    re-charges nothing already produced."""
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
        batch = aws_client("batch", config=BOTOCORE_DEFAULTS)
    except Exception as exc:  # no boto3/creds -> loud, but produce continues
        log.warning("auto-texture: boto3/clients unavailable; skipping",
                    extra={"extra_fields": {"error": str(exc)}})
        return None
    try:
        resp = batch.submit_job(
            jobName="autotexture",
            jobQueue=cfg.queue,
            jobDefinition=cfg.job_definition,
            # RUN_MODE is the job-def's default too; stated here so the job never depends on that default. No
            # SRC/window env: one job does the whole queue.
            # BLOKPORT_S3_DRY_RUN=false is REQUIRED: it defaults TRUE in dev (a safety so a box never writes the
            # bucket), and the gpu job-def does not set it -- so without this the texture upload no-ops and every
            # {Key}.png silently never reaches dev/variations/ (the produce container sets this same false).
            containerOverrides={"environment": [
                {"name": "RUN_MODE", "value": "generate-textures"},
                {"name": "BLOKPORT_S3_DRY_RUN", "value": "false"},
                # the exact per-dispatch queue this job must pull (unique per submit -> no clobber race).
                {"name": "BLOKPORT_TEXTURE_QUEUE_KEY", "value": queue_key}]})
        job_id = resp["jobId"]
        log.info("auto-texture submitted", extra={"extra_fields": {"queued": queued, "job_id": job_id}})
        return job_id
    except Exception as exc:  # a submit failure is loud but non-fatal to the produce
        log.error("auto-texture: submit_job failed",
                  extra={"extra_fields": {"queued": queued, "error": str(exc)}})
        return None
