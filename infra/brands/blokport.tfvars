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
prod_image_tag = "fade495924d04c148bd80e510513b708924c69a8" # :core w/ decided type-less card re-keyable (+ last-decision display, alias-bind guard)
prod_gpu_image_tag = "gpu-782f18c7944a3817dc211008002e9fe36e09165b" # current :gpu (reads SCRAPER_ env, prod bucket); was gpu-f9d828e0 (Aug18, pre-rename -> hit dev bucket)

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

dev_image_upgrade_batch  = 300

prod_image_upgrade_batch = 300

prod_auto_enhance     = true
prod_auto_texture     = true
prod_require_enhanced = true

# Prod runtime secrets, by SSM parameter NAME (empty = that secret is not wired).
fal_key_ssm_name       = "/blokport-prod/FAL_KEY"                 # de-watermark
scraper_proxy_ssm_name = "/blokport-prod/BLOKPORT_SCRAPER_PROXY"  # residential proxy

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "blokport-tfstate"
region                = "eu-west-1"
alert_email           = "error@blokport.com" # ops alert address for prod alarms
