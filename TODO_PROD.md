# Production setup checklist

Everything that must be done to run the pipeline in **production**. Dev is proven and deployed;
prod is **not provisioned yet** (the whole prod stack is gated on `prod_staging_bucket`). Promotion
dev->prod is a config/env change, never a code edit (see `DEV_PROD_PIPELINE.md` for the data flow).

The code fails loud on the values that have NO fallback, so a missed item errors rather than shipping
silently. Work top to bottom.

## 1. Provision the prod infra (Terraform)
The full prod stack is now DEFINED and count-gated on `prod_staging_bucket`: `scraper_prod`,
`gpu_enhance_prod` (now with FAL_KEY via `local.prod_ssm_secrets`), and `sync_service_prod` (the
produce + `/sync/v1` + `/config/v1` control plane). All three are **inert** until prod is enabled --
a dev apply never reads the prod platform state or the `/blokport-prod/` params. Activating prod is
now config + the cross-team prerequisite below, not new module code.

CROSS-TEAM PREREQUISITE (do first): `sync_service_prod` runs INSIDE Blokport's prod platform VPC.
- [ ] **Blokport prod platform up + remote state available** at `blokport/prod/terraform.tfstate`
      (bucket `blokport-tfstate`), exposing the same outputs as dev (`vpc_id`, `private_subnet_ids`,
      `ecs_cluster_arn`, `service_sg_id`, `internal_namespace_id`). `sync_service_prod` reads these via
      `data.terraform_remote_state.platform_prod` once prod is enabled.
- [ ] **Create prod SSM params:** `/blokport-prod/BLOKPORT_SYNC_TOKEN`, `/blokport-prod/BLOKPORT_CONFIG_TOKEN`
      (read by name when prod is enabled), and set `fal_key_ssm_name` (+ `scraper_proxy_ssm_name` if used) to
      the prod FAL_KEY / proxy SSM params so `local.prod_ssm_secrets` wires them into the prod scraper, GPU,
      and produce.
- [ ] Set `prod_staging_bucket` in `infra/terraform.tfvars` -- this flips `local.prod_enabled` and creates
      ALL prod resources at once (`scraper_prod`, `gpu_enhance_prod`, `sync_service_prod` + prod data sources).
- [ ] Set the prod flags as desired: `prod_image_tag`, `prod_gpu_image_tag`, `prod_schedule_enabled`,
      `prod_auto_enhance` / `prod_auto_texture` / `prod_require_enhanced` (all default OFF for a quiet start).
- [ ] `alert_email` in `terraform.tfvars` (also makes the DEV alert persistent -- a full apply otherwise
      removes the dev SNS alert created by the earlier targeted apply).
- [ ] IAM parity: the prod scrape + produce roles get the `batch:SubmitJob` bare+`:*` allowance from the
      module (same as dev); confirm on apply.
- [ ] Reconcile the pre-existing ECR state drift (root `aws_ecr_repository` "refactored but never applied")
      before a clean prod apply.
- [ ] `terraform plan` -- with the prod platform state present it should now create the prod services; then
      `apply` and confirm the prod ECS services (incl. `sync_service_prod`) + Batch queue/jobdef come up.

## 2. Prod environment variables (on the prod ECS task defs)
- [ ] `BLOKPORT_ENV=production` (selects prod bucket/keys/ids). **Enforced:** prod refuses to fall back
      to the dev bucket.
- [ ] `BLOKPORT_S3_BUCKET=<prod bucket>`. **Enforced:** prod raises at config-load if unset (never
      defaults to the dev bucket).
- [ ] `BLOKPORT_SALES_CHANNEL_ID=<prod sales channel>`. **Single id per env, no fallback.** **Enforced:**
      a prod run refuses to proceed if unset (would emit channel-less = invisible products). Dev uses its
      committed dev default; prod MUST set this.
- [ ] `BLOKPORT_S3_DRY_RUN` -- **defaults to `false` in prod now** (dev defaults `true`). Only set it
      explicitly if you deliberately want a dry prod run.
- [ ] `FAL_KEY` on the prod produce/GPU tasks (SSM secret), for FLUX texture gen + FAL de-watermark.
- [ ] `BLOKPORT_AUTO_TEXTURE=true` / `BLOKPORT_AUTO_ENHANCE=true` for prod if you want the automated
      texture + enhance loops (default off for prod).

## 3. Per-scraper config (in the :4200 config admin, per env)
- [ ] **`company_id` is set PER SCRAPER** (each source's Medusa company id, ENV-SPECIFIC), NOT globally.
      When a scraper sets it, the global default is NOT used (`source_cfg.company_id or backend.company_id`
      -- per-source wins). A scraper left empty resolves by **vendor name** on Medusa's side, so a global
      company id is intentionally NOT required for a valid prod run.
- [ ] Category `pcat` ids for prod (Slabs / Blocks / Tiles): a category is active once its prod pcat id is
      set. Add the `category,<Cat>,<prod_pcat>` rows (see `DEV_PROD_PIPELINE.md`).
- [ ] Confirm each enabled source's prod origin/ports/vendor config is correct for prod.

## 4. Seed / ledger bootstrap for prod
- [ ] The committed seed (`catalog_source/*`, `variants_export_base.csv`) is env-agnostic and now a proven
      fixed point, so a prod cold start seeds from it directly.
- [ ] Bootstrap the prod ledger from the prod Medusa exports (`from_medusa/production/*`) so ids resolve
      against the prod backend, not dev.

## 5. Blokport (Medusa) prod side
- [ ] Deploy the prod `/sync/v1` consumer, including the `port_ids` change (port of origin = the supplier
      ports the scraper sends, NOT derived from `origin_country_code`).
- [ ] Deploy the new-variant review UI + connectors on prod.
- [ ] Confirm the prod company/sales-channel/pcat ids above match the prod Medusa entities.

## 6. Verify before going live
- [ ] `python -m stone_pipeline.reference.seed verify` -> `fixed_point: True`.
- [ ] A prod dry-run produce (temporarily `BLOKPORT_S3_DRY_RUN=true`) -> inspect the staged output.
- [ ] Then a real prod produce; confirm products import with the right company (per scraper), sales
      channel, categories, ports, and images.
- [ ] Confirm the pull round-trip mints ids and products become visible.

## Notes / non-blocking (track, not launch-blockers)
- Split-type authority: a confidently-wrong scraped/resolved type can bind a homonym to the wrong-type
  variety (input-dependent, documented). Watch `tree_uncovered_variations.csv` / review holds.
- Concurrency edges: an ECS reset can race a running produce; the local-run watcher can wedge the run slot
  on a non-timeout exception. Low frequency; worth hardening later.
- On an S3 outage mid-produce, the image link/gate fail open (narrow window).
