variable "target_env" {
  type        = string
  description = "development | production -- hard-wired as BLOKPORT_ENV for this instance."
}

variable "image_repo_url" {
  type        = string
  description = "Shared ECR repo URL (root-owned). Same image as the batch task, promoted by tag."
}

variable "image_tag" {
  type        = string
  default     = "core"
  description = "ECR image tag (core | git sha)."
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "cpu" {
  type    = number
  default = 1024
}

variable "memory" {
  type    = number
  default = 2048
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "staging_bucket" {
  type        = string
  description = "This env's staging bucket (the produce writes it). Task IAM is scoped to it only."
}

# --- platform handles (from the platform remote state; see root wiring) ------
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "ecs_cluster_arn" { type = string }

variable "medusa_service_sg_id" {
  type        = string
  description = "The SG Medusa's tasks run in; we allow it ingress on 8723/8724."
}

variable "internal_namespace_id" {
  type        = string
  description = "Cloud Map private DNS namespace id (blokport-<env>.internal)."
}

variable "service_dns_name" {
  type        = string
  default     = "scraper"
  description = "Cloud Map service name -> <this>.<namespace>. Medusa hits :8723 (sync) / :8724 (config)."
}

# --- token secrets (SSM SecureString ARNs) -----------------------------------
variable "sync_token_ssm_arn" {
  type        = string
  description = "SSM SecureString ARN for BLOKPORT_SYNC_TOKEN."
}

variable "config_token_ssm_arn" {
  type        = string
  description = "SSM SecureString ARN for BLOKPORT_CONFIG_TOKEN."
}

variable "produce_secret_arns" {
  type        = map(string)
  default     = {}
  description = "ENV_NAME -> SSM ARN for the produce subprocess (e.g. BLOKPORT_SCRAPER_PROXY for the Cloudflare-fronted scrapers, FAL_KEY for image gen). Injected on the config container, which runs the live scrape. Empty when not configured."
}

variable "gpu_job_queue_name" {
  type        = string
  default     = ""
  description = "Batch job-queue NAME the produce auto-enhance trigger submits to (from the gpu_enhance module). Empty = auto-enhance never submits."
}

variable "gpu_job_definition_name" {
  type        = string
  default     = ""
  description = "Batch job-definition NAME for the enhance reprocess (from the gpu_enhance module)."
}

variable "gpu_job_queue_arn" {
  type        = string
  default     = ""
  description = "ARN of the enhance job queue. Scopes the task role's batch:SubmitJob. Empty = the permission is not granted."
}

variable "gpu_job_definition_arn" {
  type        = string
  default     = ""
  description = "ARN of the enhance job definition. Scopes the task role's batch:SubmitJob."
}

variable "auto_enhance_enabled" {
  type        = bool
  default     = false
  description = "BLOKPORT_AUTO_ENHANCE on the produce container. Ships false (dark); flip to true to auto-fire the GPU reprocess on new images."
}

variable "require_enhanced_enabled" {
  type        = bool
  default     = false
  description = "BLOKPORT_REQUIRE_ENHANCED: hard publish gate -- only GPU-enhanced images (enhanced/ marker) are linked. Ships false; enable only after markers are backfilled, else all images hold."
}
