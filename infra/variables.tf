variable "region" {
  type    = string
  default = "eu-west-1"
}

# --- Staging buckets (one per env; the prod task's IAM is scoped to prod only) -
variable "dev_staging_bucket" {
  type    = string
  default = "blokport-dev-staging-3e58a6"
}

variable "dev_enabled" {
  type        = bool
  default     = true
  description = "Whether this brand has a dev deployment. True for blokport (the one shared dev). A prod-only brand (wudport, calcport) sets this false so ONLY its prod stack stands up -- no per-brand dev."
}

variable "prod_staging_bucket" {
  type        = string
  default     = ""
  description = "Prod staging bucket. Empty = the prod task is NOT created. Set it to stand prod up."
}

variable "prod_home_env" {
  type        = string
  default     = "prod"
  description = "Which platform VPC/cluster hosts the PROD task (blokport-<prod_home_env>). Requires that platform stack to exist (blokport-prod-vpc + blokport/prod/terraform.tfstate)."
}

# --- Image (build once, promote the SAME tag dev -> prod) --------------------
variable "image_tag" {
  type        = string
  default     = "core"
  description = "ECR tag the DEV task runs (core | imageproc | a git sha)."
}

variable "prod_image_tag" {
  type        = string
  default     = "core"
  description = "ECR tag the PROD task runs. Promote the dev-proven tag here; do NOT build prod separately (avoids catalog/logic drift)."
}

variable "gpu_image_tag" {
  type        = string
  default     = "gpu"
  description = "ECR tag the DEV GPU Batch enhancer runs (:gpu tracks the latest build)."
}

variable "prod_gpu_image_tag" {
  type        = string
  default     = "gpu"
  description = "ECR tag the PROD GPU Batch enhancer runs. Promote the dev-proven :gpu-<sha> here."
}

# --- Schedules (per env; both start disabled — enable when proven) ------------
variable "dev_schedule_enabled" {
  type    = bool
  default = false
}

variable "prod_schedule_enabled" {
  type    = bool
  default = false
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 3 * * ? *)" # daily 03:00 UTC
}

# --- Sizing ------------------------------------------------------------------
variable "cpu" {
  type    = number
  default = 1024
}

variable "memory" {
  type        = number
  default     = 4096
  description = "Fargate task memory (MiB). Raise to 8192 for the imageproc (de-watermark) image."
}

variable "keep_scraped" {
  type    = string
  default = "true"
}

# --- Shared ECR lifecycle + CI deploy role ----------------------------------
variable "keep_last_images" {
  type    = number
  default = 10
}

variable "github_repo" {
  type    = string
  default = "Fux002/blokport_scraper"
}

variable "github_deploy_ref" {
  type        = string
  default     = "refs/heads/main"
  description = "Git ref allowed to assume the CI deploy role via OIDC."
}

# --- Optional secrets (by SSM name) -----------------------------------------
variable "fal_key_ssm_name" {
  type        = string
  default     = ""
  description = "SSM SecureString name for FAL_KEY. Empty = not injected."
}

variable "scraper_proxy_ssm_name" {
  type        = string
  default     = ""
  description = "SSM SecureString name for the residential proxy URL. Empty = not injected."
}

variable "dev_auto_enhance" {
  type        = bool
  default     = true
  description = "Enable BLOKPORT_AUTO_ENHANCE on the dev produce + scheduled scrape: auto-submit the GPU reprocess for newly-staged images. ON in dev: the cold-start cycle is validated. Prod uses its own var."
}

variable "dev_auto_texture" {
  type        = bool
  default     = true
  description = "Enable BLOKPORT_AUTO_TEXTURE on the dev produce: after a produce queues new-variant textures, auto-submit the GPU job (RUN_MODE=generate-textures) to generate + upload them. ON in dev: the :gpu image now carries ben2 + the baked BEN2 model, and the flag was applied + validated (2026-07-17). Prod uses its own var."
}

variable "dev_require_enhanced" {
  type        = bool
  default     = true
  description = "Enable BLOKPORT_REQUIRE_ENHANCED on dev produce + scheduled scrape: publish ONLY GPU-enhanced images. ON in dev: the enhanced/ markers are backfilled for the existing set. Prod uses its own var."
}

# Prod auto-flags: default OFF so a freshly-provisioned prod stack is quiet until deliberately enabled.
variable "prod_auto_enhance" {
  type        = bool
  default     = false
  description = "Enable BLOKPORT_AUTO_ENHANCE on the PROD produce (auto-submit the GPU reprocess for newly-staged images). Off until prod is validated."
}

variable "prod_auto_texture" {
  type        = bool
  default     = false
  description = "Enable BLOKPORT_AUTO_TEXTURE on the PROD produce (auto-submit the GPU texture job for new-variant textures). Off until the prod :gpu image + FAL_KEY are wired and validated."
}

variable "prod_require_enhanced" {
  type        = bool
  default     = false
  description = "Enable BLOKPORT_REQUIRE_ENHANCED on the PROD produce (publish ONLY GPU-enhanced images). Off until the prod enhanced/ markers are backfilled."
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Ops email for a proactive alert when an auto-texture GPU job FAILS (both envs). Empty = no alerting is created. On first apply with a value set, AWS sends an SNS confirmation email that must be clicked before alerts deliver."
}

# --- Brand + product-type (multi-brand root) ---------------------------------
# The BRAND this root stack serves and the PRODUCT pack it runs. Default blokport/stone, so the existing
# stack renders byte-identical. A second brand is a SEPARATE root stack: set brand + domain_pack + its prod
# owner ids, and init with -backend-config="key=<brand>/scraper/terraform.tfstate" (backend keys can't take
# a variable). Every blokport-named resource / tag / SSM path / platform-state key below derives from brand.
variable "brand" {
  type        = string
  default     = "blokport"
  description = "The brand this deployment serves (blokport | wudport | calcport | ...). Drives resource names, tags, SSM namespace, and the platform-state key so two brands never collide in one account."
}

variable "domain_pack" {
  type        = string
  default     = "stone"
  description = "SCRAPER_DOMAIN_PACK -- the product-domain pack this brand runs (stone | wood | lime | ...). Selects the whole product model at runtime; no code edit."
}

variable "prod_sales_channel_id" {
  type        = string
  default     = ""
  description = "SCRAPER_SALES_CHANNEL_ID injected into the PROD task (this brand's Medusa storefront). Required by the prod owner-id guard once prod is enabled."
}


variable "platform_state_bucket" {
  type        = string
  default     = "blokport-tfstate"
  description = "S3 bucket holding the PLATFORM terraform state this brand's tasks read (VPC/cluster/SG outputs). Per-brand platform -> set to that brand's state bucket. The scraper's OWN backend bucket is in backend.tf (a backend block can't take a variable; a 2nd brand overrides it with -backend-config)."
}
