#!/usr/bin/env bash
# Container entrypoint for the scheduled scraper run.
#
#   scrape every source  ->  run the pipeline (incl. Stage 7 image staging to S3)
#   ->  build the catalog ->  push the produced CSVs to S3.
#
# Image enhancement / de-watermark / S3 mode are all controlled by BLOKPORT_*
# env vars set on the Fargate task (see infra/ and DEV_PROD_PIPELINE.md). The
# Medusa tree step is intentionally NOT run here: it needs the post-upload
# variants-export refresh, which is a human gate. Run it after importing.
set -euo pipefail

# RUN_MODE selects what this task does (default = the full pipeline):
#   pipeline            scrape -> pipeline -> catalog -> upload   (default)
#   validate-dewatermark   de-watermark a sample of existing scraped/ originals and
#                          write before/after pairs to S3 (no scrape) — eyeball gate.
RUN_MODE="${RUN_MODE:-pipeline}"
if [ "$RUN_MODE" = "validate-dewatermark" ]; then
  echo "==> de-watermark validation sample (no scrape)"
  exec python -m deploy.validate_dewatermark
fi

ENV_NAME="${BLOKPORT_ENV:-development}"
echo "==> blokport scraper run | env=${ENV_NAME} image_mode=${BLOKPORT_IMAGE_MODE:-passthrough} processing=${BLOKPORT_IMAGE_PROCESSING:-false}"

echo "==> [1/5] fetch Medusa export inputs from S3 (<env>/scraper/from_medusa/)"
python -m deploy.fetch_inputs

echo "==> [2/5] scrape all sources"
python -m scrapers.run all

echo "==> [3/5] pipeline (validate, match, derive, stage images to S3)"
python -m stone_pipeline.run all

echo "==> [4/5] build catalog (to_upload/*.csv)"
python -m stone_pipeline.catalog

echo "==> [5/5] push produced artifacts to S3 (<env>/scraper/to_upload/)"
python -m deploy.upload_artifacts

echo "==> done. Review the staged output in S3, then upload variants + refresh"
echo "    the Medusa export and run 'python -m stone_pipeline.tree' before import."
