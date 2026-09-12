#!/usr/bin/env bash
# Container entrypoint for the AD-HOC scraper task (aws ecs run-task with RUN_MODE set) and the GPU
# Batch job. The produce itself has ONE path: the config container's runner (POST /config/v1/run), which
# streams, watches and gates it and writes through to the ledger. This entrypoint never produces.
set -euo pipefail

# RUN_MODE selects what this task does (there is no default: an unset mode is refused, and `pipeline`
# names the produce, which runs only in the sync service):
#   validate-dewatermark   de-watermark a sample of existing scraped/ originals and
#                          write before/after pairs to S3 (no scrape); an eyeball gate.
#   reprocess           re-run enhance/de-watermark on a source's scraped/ originals,
#                       writing into improved/ in place (no scrape). Slice with
#                       SLICE_OFFSET / SLICE_COUNT to parallelise across tasks.
RUN_MODE="${RUN_MODE:-}"
# Explicit dispatch: an UNKNOWN or unset mode must fail loud, never silently fall through to anything.
case "$RUN_MODE" in
  pipeline)
    echo "ERROR: RUN_MODE=pipeline is not a mode of this task: the produce runs in the sync service" \
         "(POST /config/v1/run on the config container), the one path with the ledger and the gates" >&2
    exit 2 ;;
  validate-dewatermark)
    echo "==> de-watermark validation sample (no scrape)"
    exec python -m deploy.validate_dewatermark ;;
  reprocess)
    echo "==> reprocess a source's scraped/ originals -> improved/ (no scrape)"
    exec python -m deploy.reprocess_source ;;
  generate-textures)
    echo "==> generate new-variant textures (FLUX.2 -> BEN2 -> dev/variations/), no scrape"
    exec python -m stone_pipeline.prepare_variant_images ;;
  *)
    echo "ERROR: unknown RUN_MODE='$RUN_MODE' (expected: validate-dewatermark | reprocess | generate-textures)" >&2
    exit 2 ;;
esac
