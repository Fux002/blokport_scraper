# ============================================================================
# WUDPORT -- wood. PROD-ONLY, ON STANDBY (no dev; no AWS resources until launch).
# ============================================================================
# As written this applies to ZERO resources: dev_enabled=false disables the dev
# stack, and prod_staging_bucket="" keeps prod un-provisioned. To LAUNCH, fill the
# FILL_ME values below, then:
#   terraform init -reconfigure -backend-config="bucket=wudport-tfstate" \
#                               -backend-config="key=wudport/scraper/terraform.tfstate"
#   terraform apply -var-file=brands/wudport.tfvars
#
# Prereqs before launch (real onboarding, not just a deploy):
#   - the wood domain pack (config/domains/wood.yaml) filled out (currently a skeleton),
#   - wudport's own supplier scrapers + adapters built,
#   - wudport's platform (VPC/cluster) + platform state bucket standing.
# ----------------------------------------------------------------------------

brand       = "wudport"
domain_pack = "wood"

# --- NO DEV for this brand (prod-only model) --------------------------------
dev_enabled = false

# --- PROD (standby until these are filled) ----------------------------------
prod_staging_bucket   = "" # FILL to launch: wudport-prod-staging-<suffix>
prod_sales_channel_id = "" # FILL: wudport prod Medusa sales channel id
prod_home_env         = "prod"

fal_key_ssm_name       = "" # FILL: /wudport-prod/FAL_KEY
scraper_proxy_ssm_name = "" # FILL: /wudport-prod/BLOKPORT_SCRAPER_PROXY

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "wudport-tfstate" # wudport's own platform state
region                = "eu-west-1"
alert_email           = ""
