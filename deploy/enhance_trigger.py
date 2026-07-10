"""Auto-enhance trigger: after a produce stages new raw images, submit the GPU reprocess for just the
NEW ones, so enhancement/de-watermark happens automatically (GPU spins up on demand and back to zero).

The "new" set is the DELTA: images in scraped/<src>/ that have neither an ENHANCED nor a DISCARDED marker
(the two "done" signals the GPU reprocess writes). We deliberately do NOT use improved/ presence: produce
on the torch-free :core writes a raw re-encode into improved/ without enhancing, so improved/ presence is
not "enhanced". Only the GPU writes the markers, so the delta is immune to what produce does.

Fire-and-forget + flag-gated: off unless BLOKPORT_AUTO_ENHANCE is set; a submit failure logs loud and
never crashes the produce (the images just stay raw until the next trigger). The submitted reprocess runs
with CLASSIFY=false -- auto-enhance never auto-discards (that needs a calibrated margin, done separately).

Shared with deploy/reprocess_source: `done_shas()` is the incremental "already processed" set.
"""

from __future__ import annotations

import os

from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.io import imagestore

log = logfmt.get_logger("enhance_trigger")


def _keys_under(client, prefix: str) -> list[str]:
    """All object keys under an S3 prefix (unsorted)."""
    out: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []))
    return out


def _shas_under(client, prefix: str) -> set[str]:
    """All content sha256 under an S3 prefix (from the <sha>.<ext> object names)."""
    return {sha for k in _keys_under(client, prefix) if (sha := imagestore.sha_from_url(k))}


def done_shas(client, source: str) -> set[str]:
    """Images the GPU reprocess has already handled for `source`: enhanced markers + discarded markers.
    An image is "done" (no reprocessing needed) iff its sha is in this set."""
    return _shas_under(client, imagestore.enhanced_prefix(source)) | \
        _shas_under(client, imagestore.discarded_prefix(source))


def pending_shas(client, source: str) -> set[str]:
    """The DELTA: scraped originals for `source` that are neither enhanced nor discarded yet."""
    return _shas_under(client, imagestore.scraped_prefix(source)) - done_shas(client, source)


def _watermarked_sources() -> set[str]:
    from stone_pipeline.config.sources import load_sources

    return {name for name, cfg in load_sources().items() if getattr(cfg, "watermarked", False)}


def submit_pending(sources: list[str] | None = None) -> list[str]:
    """For each source with un-enhanced originals, submit the GPU reprocess (auto-sliced). Returns the
    submitted job ids. No-op (returns []) when disabled, misconfigured, or nothing is pending."""
    cfg = SETTINGS.auto_enhance
    if not cfg.enabled:
        log.info("auto-enhance disabled (BLOKPORT_AUTO_ENHANCE unset); no GPU job submitted")
        return []
    if not cfg.queue or not cfg.job_definition:
        log.warning("auto-enhance enabled but queue/job-definition not configured; skipping",
                    extra={"extra_fields": {"queue": cfg.queue, "job_definition": cfg.job_definition}})
        return []
    try:
        import boto3

        s3 = boto3.client("s3", region_name=S3_REGION)
        batch = boto3.client("batch", region_name=S3_REGION)
    except Exception as exc:  # no boto3/creds -> loud, but produce continues
        log.warning("auto-enhance: boto3/clients unavailable; skipping",
                    extra={"extra_fields": {"error": str(exc)}})
        return []

    from stone_pipeline.config.sources import load_sources
    srcs = sources if sources is not None else list(load_sources().keys())
    watermarked = _watermarked_sources()
    size = cfg.slice_size
    submitted: list[str] = []
    for src in srcs:
        try:
            scraped = sorted(_keys_under(s3, imagestore.scraped_prefix(src)))  # STABLE list, == reprocess sort
            done = done_shas(s3, src)
        except Exception as exc:  # a listing failure for one source never blocks the others
            log.warning("auto-enhance: could not list source; skipping",
                        extra={"extra_fields": {"source": src, "error": str(exc)}})
            continue
        # Windows (offset/size buckets) of the STABLE full sorted list that contain at least one un-done
        # image. We submit offsets into the FULL list (not the delta) so the reprocess -- which slices that
        # same stable list -- never misaligns under concurrency; it skips already-done images inside each
        # window per-image. Submitting only non-empty windows keeps the fan-out cheap (no idle GPU jobs).
        pending_positions = [i for i, k in enumerate(scraped)
                             if (imagestore.sha_from_url(k) or "") not in done]
        if not pending_positions:
            continue
        windows = sorted({p // size for p in pending_positions})
        for w in windows:
            offset = w * size
            try:
                resp = batch.submit_job(
                    jobName=f"autoenh-{src}-{offset}",
                    jobQueue=cfg.queue,
                    jobDefinition=cfg.job_definition,
                    containerOverrides={"environment": [
                        {"name": "SRC", "value": src},
                        {"name": "WATERMARKED", "value": "true" if src in watermarked else "false"},
                        {"name": "CLASSIFY", "value": "false"},   # auto-enhance never auto-discards
                        {"name": "SLICE_OFFSET", "value": str(offset)},
                        {"name": "SLICE_COUNT", "value": str(size)},
                    ]})
                submitted.append(resp["jobId"])
            except Exception as exc:  # a submit failure is loud but non-fatal to the produce
                log.error("auto-enhance: submit_job failed",
                          extra={"extra_fields": {"source": src, "offset": offset, "error": str(exc)}})
        log.info("auto-enhance submitted", extra={"extra_fields": {
            "source": src, "pending": len(pending_positions), "windows": len(windows)}})
    return submitted


if __name__ == "__main__":
    # manual trigger: BLOKPORT_AUTO_ENHANCE=1 BLOKPORT_GPU_QUEUE=... BLOKPORT_GPU_JOBDEF=... python -m deploy.enhance_trigger [src ...]
    import sys

    ids = submit_pending(sys.argv[1:] or None)
    print(f"submitted {len(ids)} job(s): {ids}")
