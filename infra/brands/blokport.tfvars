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

# Immutable, dev-proven image tags promoted to prod (NEVER the mutable core/gpu).
# core = current dev :core (this session's fixes, live-verified); gpu = latest built GPU image.
prod_image_tag     = "586939a745a40ee7fee96da59356b3e9eb55d221"     # + a stock-only refresh never rewrites a product, a failed source fails loud (#481)
prod_gpu_image_tag = "gpu-f0af3849c268042e7422006cb6de5ac593fcb1aa" # enhance fits the input to the target before ESRGAN (#448): ~4x less GPU work on large originals

# Image processing = DEV PARITY (all on). Enhancing/de-watermarking is toggled per-source LIVE via the
# :4200 admin UI (source.enhance / source.watermarked), NOT these infra flags -- these just make prod's
# task config identical to dev so the UI behaves the same. prod gpu image (gpu-782f18c7) == dev's :gpu
# digest sha256:20767ddd -> carries ben2 + reads SCRAPER_ env, so auto_texture + prod bucket are safe.
# Texture quality drip: per produce, re-make up to N legacy textures on the current best model, ONLY for
# variants a product actually links to, once each (durable S3 marker). 257 product-backed textures are on
# the old model today, so 300 clears the backlog in one produce and then idles at ~0 (each Key once).
# Operator shell into the dev sync task (ECS Exec), so the ledger can be reset/inspected without the UI.
# Dev only, on purpose: prod has no equivalent and its reset stays a UI/API action behind the run lock.
dev_enable_execute_command = true

dev_image_upgrade_batch = 1000

prod_image_upgrade_batch = 1000

# Produce ceiling, both envs (the runner kills a run past it). 4 h: a full live scrape with fresh images
# passed 2 h on 2026-09-14 and was killed as wedged.
run_timeout_seconds = 14400

# The old enhancer (GPU upscale + FAL de-watermark) is OFF in both envs (owner 2026-11-02): the standardised photo
# pipeline replaces its output, and a product is live on prod only with a standardised photo (no_photo rule). A
# watermarked source's logo is taken off by the pipeline's hosted clean-up. Its code goes in a later clean-up.
dev_auto_enhance      = false
dev_require_enhanced  = false
prod_auto_enhance     = false
prod_auto_texture     = true
prod_require_enhanced = false

# A product without a standardised photo is hidden (qty 0, reason no_photo) and kept for review. Prod has it by the
# code's production default; dev sets it so dev shows what prod shows (owner 2026-11-02).
dev_require_standard_photo = true

# The standardised-photo process (STANDARDISE_PROCESS.md): the GPU image both envs' standardise jobs run today
# (standardise-edit-cbc576cf9ebd-x, registered by hand until now), under its protected pin tag: add the tag to that
# image in ECR BEFORE applying (STANDARDISE_PROCESS.md, rollout). The process runs after each full produce only once
# AUTO is on (off in both until a dev run is verified). The month's budget, hosted calls and GPU together, is the
# owner's (2026-11-02): prod 100 USD, dev 20 USD.
dev_standardise_image_tag  = "standardise-pin-cbc576cf9ebd"
prod_standardise_image_tag = "standardise-pin-cbc576cf9ebd"
dev_standardise_auto       = false
prod_standardise_auto      = false
# The hosted clean-up edits (FAL) stay off in both until the owner switches them on (owner 2026-11-02: no FAL spend now);
# listings whose photos all fail for a cleanable reason go to the review list instead.
dev_standardise_clean       = false
prod_standardise_clean      = false
dev_standardise_budget_usd  = "20"
prod_standardise_budget_usd = "100"
dev_standardise_gpu_vcpus   = 32
prod_standardise_gpu_vcpus  = 128

# Prod runtime secrets, by SSM parameter NAME (empty = that secret is not wired).
fal_key_ssm_name       = "/blokport-prod/FAL_KEY"                # de-watermark
scraper_proxy_ssm_name = "/blokport-prod/BLOKPORT_SCRAPER_PROXY" # residential proxy

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "blokport-tfstate"
region                = "eu-west-1"
alert_email           = "error@blokport.com" # ops alert address for prod alarms
