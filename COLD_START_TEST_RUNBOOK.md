# True cold-start test runbook (dev)

A coordinated, from-zero validation of the whole loop: reset to zero sync state, re-serve every variety
via a full 4-source produce, sync into Medusa, load combinations, and confirm the storefront shows the
full catalog with products + images. This is the shared artifact for the scraper chat, the image/infra
chat (Blokport), and the operator.

Scope decision (locked): TRUE zero-reset (re-mint all), proxy wired first. "Zero" means zero SYNC STATE,
not zero entities: the hard reset preserves the ledger variations + backbone and the S3 variety textures
(12,529 on dev/variations/), so the bulk re-serve reuses existing textures. FAL is needed only for
net-new varieties a fresh scrape discovers; it is confirmed wired anyway per COLD_START.md.

Owners: [SCRAPER] = the scraper chat (verify + roll + verify). [BLOKPORT] = infra/image/Medusa chat.
[OPERATOR] = the two buttons on :4200.

## Prerequisites verified on the scraper side (ready)

- Service steady on the reconciled task def, s3 image mode, running :core with PR #19 (combination publish
  on produce + delta-baseline restore on cold start).
- attributes.csv present on S3 (dev/scraper/from_medusa/attributes.csv, carries the category pcats) so the
  typing serve-gate is satisfied, PROVIDED the reset keeps Medusa's attribute definitions.
- 12,529 variety textures already on dev/variations/ for reuse.
- Cold-restore proven live (ledger + config.db + artifact trees + combinations baseline all restore).

## Phase 0: prep (MUST precede the reset)

Gates everything. Nothing below runs until these land and the service rolls. Ownership split by DOMAIN:
the proxy + FAL are SCRAPER infra (the scraper repo's terraform); only the attribute-definition guard is
Medusa/Blokport. FAL / texture generation is NOT a Blokport concern (COLD_START.md: entirely scraper-side).

Phase 0a: SCRAPER infra (whoever applies the scraper terraform / sync_service module). Both secrets already
exist in SSM; injection is gated only by two root terraform vars that are currently unset (default ""):

  fal_key_ssm_name       = "/blokport-dev/FAL_KEY"                 # texture gen for net-new varieties
  scraper_proxy_ssm_name = "/blokport-dev/BLOKPORT_SCRAPER_PROXY"  # Cloudflare-fronted per-product fetches

Setting both feeds local.ssm_secrets -> produce_secret_arns -> injected as FAL_KEY + BLOKPORT_SCRAPER_PROXY
on the sync_service CONFIG container (main.tf module.sync_service_dev). Then:
  terraform apply -target=module.sync_service_dev   # new task-def revision with both secrets
and let the service roll. (They were dropped/never-set: rev 14 carries neither.)

Phase 0b: MEDUSA / Blokport. Confirm the clean-reset preserves the attribute definitions (colour / finish /
type / quality). "Zero" is zero sync state, not zero vocabulary. If the reset wipes attributes, nothing
types and nothing serves.

[SCRAPER-pipeline] After the 0a roll: re-verify the live task carries s3-mode + BLOKPORT_SCRAPER_PROXY +
FAL_KEY, and attributes.csv is still present. Only then proceed.

## Phase 1: coordinated reset (the reset contract)

1. [BLOKPORT] Reset Medusa to zero sync state, keeping attribute definitions + base variations.
2. [OPERATOR] :4200 -> Reset (hard). Clears the scraper ledger sync overlay + drops scraped products.
   Variation + backbone rows are preserved (reset contract).
3. [SCRAPER] Force-roll the ECS task cold. Verify the startup log shows a clean restore (config.db, ledger,
   outputs + data trees, combinations baseline) and "config server listening", no errors, s3-mode.

## Phase 2: full produce (the re-serve)

1. [OPERATOR] :4200 -> Run, sources = all, stage = all. Runs fetch inputs -> scrape all 4 -> pipeline ->
   catalog -> publish.
2. [SCRAPER] Watch live and verify:
   - products emit > 0 (the no_image wall is cleared: s3-mode re-hosts product images and repopulates
     dev/products/_manifest.json; existing varieties reuse their textures).
   - the 3 combination files + per-source products publish FRESH to dev/scraper/to_upload/ with CURRENT
     variation ids.
   - flag any FAL-held net-new varieties and any thin/blocked source.

## Phase 3: Medusa sync + import

1. [BLOKPORT] Medusa pulls variations (re-mints ids -> acks) -> products -> inventory.
2. [BLOKPORT] Once variations are synced, :4200 (or admin) -> Load full library (combinations, REPLACE) ->
   valid_combination fills.
3. [ALL] Verify the configurator / storefront shows the full catalog with products + images.

## Phase 4: enhancement (async, after)

1. [BLOKPORT] GPU Batch reprocess (:gpu) de-watermarks + upscales over scraped/ in place. Products already
   serve with the hosted-but-un-enhanced images from Phase 2; this upgrades them.

## The operator's buttons (only two, on :4200)

1. Reset (hard)  -- Phase 1.
2. Run (sources = all, stage = all)  -- Phase 2.

## Verification checklist (scraper, per phase)

- P1: cold task restore clean (no "starting fresh" for the ledger/config unless intended); s3-mode active.
- P2: produce exits 0; products emitted > 0; no_image count small; to_upload/ 3 combination files + product
  files timestamped fresh with current ids; dev/products/_manifest.json non-empty.
- P3: Medusa Load-full validates (no variation_id rejects); configurator shows products.

## Abort / rollback

- Bucket versioning is on (S3 objects recoverable). The hard reset drops scraped products + sync overlay
  but not variations/backbone, so re-running Phase 2 (without a fresh reset) re-serves from the same base.
  If Phase 2 emits 0 products, do not Load anything into Medusa; diagnose (FAL / manifest / typing) first.
