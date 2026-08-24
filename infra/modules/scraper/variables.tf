variable "target_env" {
  type        = string
  description = "The environment this instance IS, hard-wired as BLOKPORT_ENV (development | production)."
  validation {
    condition     = contains(["development", "production"], var.target_env)
    error_message = "target_env must be 'development' or 'production'."
  }
}

variable "home_env" {
  type        = string
  description = "Which Medusa platform VPC/cluster HOSTS this task (blokport-<home_env>): 'dev' or 'prod'. Resolves the VPC tag and the platform remote state key."
}

variable "staging_bucket" {
  type        = string
  description = "The single staging bucket this environment reads/writes. The task IAM role is scoped to ONLY this bucket."
}

variable "image_repo_url" {
  type        = string
  description = "Shared ECR repository URL (owned by the root stack). Both envs run the SAME image, promoted by tag."
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "state_bucket" {
  type        = string
  default     = "blokport-tfstate"
  description = "S3 bucket holding the Medusa platform Terraform state (read-only, for the cluster name)."
}

variable "image_tag" {
  type        = string
  default     = "core"
  description = "ECR image tag this env runs (core | imageproc | a git sha). Promote the SAME tag dev -> prod."
}

variable "cpu" {
  type    = number
  default = 1024
}

variable "memory" {
  type    = number
  default = 4096
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 3 * * ? *)" # daily 03:00 UTC
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Start the cron disabled — run the task manually first, enable when proven."
}

variable "keep_scraped" {
  type    = string
  default = "true"
}

variable "ssm_secret_arns" {
  type        = map(string)
  default     = {}
  description = "Optional name -> existing SSM SecureString ARN to inject as container secrets (e.g. FAL_KEY)."
}

variable "secrets_kms_key_arn" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "gpu_job_queue_name" {
  type        = string
  default     = ""
  description = "Batch job-queue NAME the auto-enhance trigger submits to (from the gpu_enhance module). Empty = never submits."
}

variable "gpu_job_definition_name" {
  type        = string
  default     = ""
  description = "Batch job-definition NAME for the enhance reprocess (from the gpu_enhance module)."
}

variable "gpu_job_queue_arn" {
  type        = string
  default     = ""
  description = "ARN of the enhance job queue; scopes the task role's batch:SubmitJob. Empty = permission not granted."
}

variable "gpu_job_definition_arn" {
  type        = string
  default     = ""
  description = "ARN of the enhance job definition; scopes the task role's batch:SubmitJob."
}

variable "auto_enhance_enabled" {
  type        = bool
  default     = false
  description = "BLOKPORT_AUTO_ENHANCE on the scheduled scraper. Ships false (dark); flip to auto-fire the GPU reprocess on new images."
}

variable "require_enhanced_enabled" {
  type        = bool
  default     = false
  description = "BLOKPORT_REQUIRE_ENHANCED: hard publish gate -- only GPU-enhanced images (enhanced/ marker) are linked. Ships false; enable only after markers are backfilled, else all images hold."
}

# --- Brand + product-type selection (multi-brand / multi-material) ------------
# These make the deployment brand- and product-agnostic: the product type is a SETUP CHOICE (the domain
# pack), and the brand names its store. The blokport-stone stack keeps the defaults, so nothing changes;
# a wudport (wood) / calcport (lime) stack sets brand + domain_pack + its own sales channel.
variable "brand" {
  type        = string
  default     = "blokport"
  description = "BLOKPORT_BRAND: the brand this deployment serves. Cross-checked against the bucket name in prod so a bucket mis-set to another brand's store fails loud. (blokport | wudport | calcport | ...)"
}

variable "domain_pack" {
  type        = string
  default     = "stone"
  description = "BLOKPORT_DOMAIN_PACK: the product-domain pack (config/domains/<pack>.yaml) this deployment runs. stone (default) | wood | lime | ... -- selects the whole vocabulary/category/density model, no code change."
}

variable "sales_channel_id" {
  type        = string
  default     = ""
  description = "BLOKPORT_SALES_CHANNEL_ID: this brand's Medusa sales channel (storefront). One per deployment; empty falls back to the code dev default in dev, and is required (fail-loud) in prod."
}
