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
create_ecr  = false # references the shared ECR (owned by blokport); does NOT re-create it

# --- NO DEV for this brand (prod-only model) --------------------------------
dev_enabled = false

# --- PROD (standby until these are filled) ----------------------------------
prod_staging_bucket   = "" # FILL to launch: wudport-prod-staging-<suffix>
prod_sales_channel_id = "" # FILL: wudport prod Medusa sales channel id
prod_home_env         = "prod"

fal_key_ssm_name       = "" # FILL: /wudport-prod/FAL_KEY
scraper_proxy_ssm_name = "" # FILL: /wudport-prod/BLOKPORT_SCRAPER_PROXY

# --- shared / ops -----------------------------------------------------------
platform_state_bucket = "blokport-tfstate" # NOT a per-brand bucket: the PLATFORM keeps every
# brand's state in the one blokport-tfstate bucket, separated by KEY ("<brand>/<env>/terraform.tfstate")
# — wudport-dev already lives at blokport-tfstate/wudport/dev/terraform.tfstate, and wudport-prod at
# .../wudport/prod/. A "wudport-tfstate" bucket does not exist, so the platform_prod remote-state read
# would fail at plan time. This is only about where the PLATFORM's state is read from; this stack's
# OWN state still goes wherever brands/wudport.backend.hcl says.
region                = "eu-west-1"
alert_email           = ""
