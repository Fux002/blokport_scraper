# ============================================================================
# CALCPORT -- lime. PROD-ONLY, ON STANDBY (no dev; no AWS resources until launch).
# ============================================================================
# As written this applies to ZERO resources: dev_enabled=false disables the dev
# stack, and prod_staging_bucket="" keeps prod un-provisioned. To LAUNCH, fill the
# FILL_ME values below, then:
#   terraform init -reconfigure -backend-config="bucket=calcport-tfstate" \
#                               -backend-config="key=calcport/scraper/terraform.tfstate"
#   terraform apply -var-file=brands/calcport.tfvars
#
# Prereqs before launch (real onboarding, not just a deploy):
#   - the lime domain pack (config/domains/lime.yaml) filled out (currently a skeleton),
#   - calcport's own supplier scrapers + adapters built,
#   - calcport's platform (VPC/cluster) + platform state bucket standing.
# ----------------------------------------------------------------------------

brand       = "calcport"
domain_pack = "lime"
create_ecr  = false # references the shared ECR (owned by blokport); does NOT re-create it

# --- NO DEV for this brand (prod-only model) --------------------------------
dev_enabled = false

# --- PROD (standby until these are filled) ----------------------------------
prod_staging_bucket   = "" # FILL to launch: calcport-prod-staging-<suffix>
prod_sales_channel_id = "" # FILL: calcport prod Medusa sales channel id
prod_home_env         = "prod"

fal_key_ssm_name       = "" # FILL: /calcport-prod/FAL_KEY
scraper_proxy_ssm_name = "" # FILL: /calcport-prod/BLOKPORT_SCRAPER_PROXY

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "calcport-tfstate" # VERIFY BEFORE USE: must match wherever calcport's
# PLATFORM writes its state. The convention for the existing brands is a single shared bucket
# (blokport-tfstate) separated by key "<brand>/<env>/terraform.tfstate", NOT a bucket per brand —
# see wudport.tfvars. calcport has no platform stack yet, so this value is unverified.
region                = "eu-west-1"
alert_email           = ""
