# ============================================================================
# BLOKPORT -- stone. The one shared DEV + the one active PROD.
# ============================================================================
# Apply:
#   terraform init -reconfigure -backend-config="bucket=blokport-tfstate" \
#                               -backend-config="key=blokport/scraper/terraform.tfstate"
#   terraform apply -var-file=brands/blokport.tfvars
#
# brand/domain_pack/dev_staging_bucket/platform_state_bucket already default to
# these values; they are pinned here so the file is self-describing.
# ----------------------------------------------------------------------------

brand       = "blokport"
domain_pack = "stone"
create_ecr  = true # blokport OWNS the shared ECR image repo; every other brand references it

# --- DEV (the one shared dev; lives in blokport-dev) ------------------------
dev_enabled        = true
dev_staging_bucket = "blokport-dev-staging-3e58a6"

# --- PROD (the one active prod) --------------------------------------------
# Set prod_staging_bucket to a non-empty value to STAND PROD UP. Leave it "" to
# keep prod on standby (dev-only) until Blokport's prod platform state exists.
# STOOD UP 2026-09-02: Blokport delivered the prod platform state, bucket, tokens
# and the prod sales-channel id.
prod_staging_bucket   = "blokport-prod-staging"
prod_sales_channel_id = "sc_01M1GGAHFVHZ8XEY20KVZP2QV5" # Default Sales Channel, prod bootstrap
prod_home_env         = "prod"

# Immutable, dev-proven image tags promoted to prod (NEVER the mutable core/gpu; variables.tf refuses them). Each pinned
# image also carries its pin tag (prod-<sha12> / prod-gpu-<sha12>), which the ECR lifecycle keeps (DEPLOY.md).
prod_image_tag     = "b9b0831de92b1431bbc7d3a4c32d1cfa7db43900"     # the C2b candidate: 11da0088 + SC #648 (Calcite Blue = Casa Blue, owner 12 Nov), data only; built by build-candidate (run 43206336786), digest 12fcc800; pin tag prod-b9b0831de92b
prod_gpu_image_tag = "gpu-f0af3849c268042e7422006cb6de5ac593fcb1aa" # the texture job's image (built at #448)

# The image pipeline is switched per source LIVE via the :4200 admin UI (source.image_pipeline), NOT these
# infra flags.
# Operator shell into the dev sync task (ECS Exec), so the ledger can be reset/inspected without the UI.
# Dev only, on purpose: prod has no equivalent and its reset stays a UI/API action behind the run lock.
dev_enable_execute_command = true

# Texture quality drip: per produce, re-make up to N legacy textures on the current best model, ONLY for
# variants a product actually links to, once each (durable S3 marker). N is a per-produce cap: a backlog
# smaller than it drains in one produce, after which the drip idles at ~0 (each Key once).
dev_image_upgrade_batch = 1000

prod_image_upgrade_batch = 1000

# Produce ceiling, both envs (the runner kills a run past it). 4 h: a full live scrape with fresh images
# passed 2 h on 2026-09-14 and was killed as wedged.
run_timeout_seconds = 14400

# Dev off (owner 2026-11-10: no dev GPU spend, dev proves logic on tests and a few rows; Mas 2026-11-11): a dev produce
# never submits the texture GPU job. Prod keeps its own.
dev_auto_texture  = false
prod_auto_texture = true

# A product without a standardised photo is hidden (qty 0, reason no_photo) and kept for review. Prod has it by the
# code's production default; dev sets it so dev shows what prod shows (owner 2026-11-02).
dev_require_standard_photo = true

# The standardised-photo process (STANDARDISE_PROCESS.md): the GPU image each env's standardise jobs run, built by
# deploy/build_standardise_image.sh as standardise-pin-<sha12> of the commit it holds. A job definition is applied
# before its env's svc runs the matching code (the release order, STANDARDISE_PROCESS.md).
# AUTO: on in prod: the photo process follows each full produce (owner 2026-11-02: promote to prod after a clean dev run).
# Off in dev (owner 2026-11-10: no dev GPU spend; Mas 2026-11-11), so a dev produce never starts the photo GPU jobs. The
# month's budget, hosted calls and GPU together, is the owner's: prod 400 USD for November's combined pass, dev 100 USD
# (2026-11-05).
dev_standardise_image_tag  = "standardise-pin-809b0b3e2806"
prod_standardise_image_tag = "standardise-pin-eea91c517a8b"
dev_standardise_auto       = false
prod_standardise_auto      = true
# The automatic clean-up of marks (FAL) stays off in both until the owner switches it on (owner 2026-11-02); a photo the
# owner clicks is cleaned either way, within the cleaning cap below (owner 2026-11-05). Prod ran it ON once for the
# combined pass of 2026-11-05 (owner: corner recheck + clean-up of the listings with no passing photo), then off.
dev_standardise_clean       = false
prod_standardise_clean      = false
dev_standardise_budget_usd  = "100"
prod_standardise_budget_usd = "409.08" # 3D run: Nov spend to date 209.081352 (5 runs, prod_spend_to_date.sh 9046a9e3, 11-10) + 200
# The clean-up edits' own monthly cap (owner 2026-11-05: $30 prod, $10 dev), apart from the budget above. It pays for the
# owner's clicks always, and for the automatic marks only while dev_/prod_standardise_clean is on.
dev_standardise_clean_budget_usd  = "10"
prod_standardise_clean_budget_usd = "35" # 3D run: Nov clean spend to date 0 (no clean-spend file, the same read) + 35
dev_standardise_gpu_vcpus         = 32
prod_standardise_gpu_vcpus        = 128
# A scoped photos run (a scope file plus its own cap, select.RunCap): unset, every photos run works all new and changed
# photos within the monthly budgets above. The prod 3D run (PROD_3D_RUN_PLAN_v2, scope 52fd255b, cap 235) ran on
# 2026-11-11 and was unscoped afterwards (owner: "back to normal").
prod_standardise_scope_key   = ""
prod_standardise_run_cap_usd = ""
# The overnight photos run (deploy.start_photos_run, infra/modules/sync_service/photos_nightly.tf): the schedule exists
# in both environments and is off until the owner switches it on; on or off is one apply that never rolls the service.
dev_photos_nightly  = false
prod_photos_nightly = false

# Prod runtime secrets, by SSM parameter NAME (empty = that secret is not wired).
fal_key_ssm_name       = "/blokport-prod/FAL_KEY"                # texture generation + the photo process
scraper_proxy_ssm_name = "/blokport-prod/BLOKPORT_SCRAPER_PROXY" # residential proxy
# Polonine's customer login for its price pass: the produce only. Dev by the known /blokport-dev/ names.
polonine_login             = true
polonine_username_ssm_name = "/blokport-prod/SCRAPER_POLONINE_USERNAME"
polonine_password_ssm_name = "/blokport-prod/SCRAPER_POLONINE_PASSWORD"

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "blokport-tfstate"
region                = "eu-west-1"
alert_email           = "error@blokport.com" # ops alert address for prod alarms
