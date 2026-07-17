variable "region" {
  type    = string
  default = "eu-west-1"
}

# --- Staging buckets (one per env; the prod task's IAM is scoped to prod only) -
variable "dev_staging_bucket" {
  type    = string
  default = "blokport-dev-staging-3e58a6"
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
  default     = false
  description = "Enable BLOKPORT_AUTO_TEXTURE on the dev produce: after a produce queues new-variant textures, auto-submit the GPU job (RUN_MODE=generate-textures) to generate + upload them. Ships OFF: flip true ONLY after the :gpu image is rebuilt with ben2 (build_gpu), else dispatched jobs fail at background-removal."
}

variable "dev_require_enhanced" {
  type        = bool
  default     = true
  description = "Enable BLOKPORT_REQUIRE_ENHANCED on dev produce + scheduled scrape: publish ONLY GPU-enhanced images. ON in dev: the enhanced/ markers are backfilled for the existing set. Prod uses its own var."
}
