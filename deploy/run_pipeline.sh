#!/usr/bin/env bash
# Container entrypoint for the AD-HOC scraper task (aws ecs run-task with RUN_MODE set) and the GPU
# Batch job. The produce itself has ONE path: the config container's runner (POST /config/v1/run), which
# streams, watches and gates it and writes through to the ledger. This entrypoint never produces.
set -euo pipefail

# RUN_MODE selects what this task does (there is no default: an unset mode is refused, and `pipeline`
# names the produce, which runs only in the sync service). The GPU modes are submitted to Batch by the
# photo process (standardise, standardise-retouch) and by the texture trigger (generate-textures). market-prices runs
# in the ad-hoc task: the competitor-price collection for this deployment's brand (market_prices/CONTRACT.md), and
# market-prices-intake absorbs the competitor price sheets the owner puts in the brand's intake folder.
RUN_MODE="${RUN_MODE:-}"
# Explicit dispatch: an UNKNOWN or unset mode must fail loud, never silently fall through to anything.
case "$RUN_MODE" in
  pipeline)
    echo "ERROR: RUN_MODE=pipeline is not a mode of this task: the produce runs in the sync service" \
         "(POST /config/v1/run on the config container), the one path with the ledger and the gates" >&2
    exit 2 ;;
  standardise)
    echo "==> standardise a source's scraped/ originals -> the test prefix (no scrape, never improved/)"
    exec python -m stone_pipeline.photos.source ;;
  standardise-close)
    echo "==> make the chosen photos' close-ups again from their full-resolution photos (no upscale, no judge)"
    exec python -m stone_pipeline.photos.close ;;
  standardise-retouch)
    echo "==> take straps, cards and stickers off a source's photos (no scrape, never improved/)"
    exec python -m stone_pipeline.photos.retouch_job ;;
  standardise-edit-test)
    echo "==> compare editing models on hard photos (dev only, nothing published)"
    exec python -m stone_pipeline.photos.edit_test ;;
  standardise-diag)
    echo "==> why the listed photos' slab outlines came out as they did -> the diag prefix (dev, nothing published)"
    exec python -m stone_pipeline.photos.diag ;;
  generate-textures)
    echo "==> generate new-variant textures (FLUX.2 -> BEN2 -> the env's variations/ prefix), no scrape"
    exec python -m stone_pipeline.prepare_variant_images ;;
  cutout-meta)
    echo "==> add what each published 3D slab is made from to its outline (the outline only; the cut-out untouched)"
    exec python -m stone_pipeline.photos.cutout_meta ;;
  phase1-migrate)
    echo "==> phase 1 store migration (SC-3): MIGRATE_WRITE=0 decides and writes nothing; 1 writes the .v2 stores"
    exec python -m stone_pipeline.phase1.migrate_run ;;
  market-prices)
    echo "==> collect competitor prices for this deployment's brand (approved sites only) -> <env>/market_prices/<brand>/"
    exec python -m market_prices.run ;;
  market-prices-intake)
    echo "==> absorb this brand's competitor price sheets from <env>/market_prices/<brand>/intake/ (each once)"
    exec python -m market_prices.intake ;;
  *)
    echo "ERROR: unknown RUN_MODE='$RUN_MODE' (expected: standardise | standardise-close | standardise-retouch | standardise-edit-test | standardise-diag | generate-textures | cutout-meta | phase1-migrate | market-prices | market-prices-intake)" >&2
    exit 2 ;;
esac
