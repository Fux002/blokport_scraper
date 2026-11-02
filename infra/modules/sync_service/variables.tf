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
  description = "Batch job-queue NAME the texture trigger and the photo process submit to (from the gpu_enhance module). Empty = the texture trigger never submits."
}

variable "gpu_job_definition_name" {
  type        = string
  default     = ""
  description = "Batch job-definition NAME for the texture job (from the gpu_enhance module)."
}

variable "gpu_job_queue_arn" {
  type        = string
  default     = ""
  description = "ARN of the GPU job queue. Scopes the task role's batch:SubmitJob. Empty = the permission is not granted."
}

variable "gpu_job_definition_arn" {
  type        = string
  default     = ""
  description = "ARN of the texture job definition. Scopes the task role's batch:SubmitJob."
}

variable "auto_texture_enabled" {
  type        = bool
  default     = false
  description = "BLOKPORT_AUTO_TEXTURE on the produce container. Ships false (dark); flip to true ONLY after the :gpu image is rebuilt with ben2, else the dispatched texture job fails at background-removal. Runs on the gpu_enhance queue/jobdef (RUN_MODE=generate-textures)."
}

variable "require_standard_photo" {
  type        = bool
  default     = null
  description = "SCRAPER_REQUIRE_STANDARD_PHOTO: a product without a standardised photo is served qty 0 with reason no_photo (hidden, kept for review). Unset (null) leaves the code's env default (on in production); prod does not pass it."
}

variable "secrets_kms_key_arn" {
  type        = string
  default     = ""
  description = "KMS key ARN for the SSM SecureStrings (grants kms:Decrypt on the execution role). Empty = AWS-managed aws/ssm key (implicit decrypt); set to the customer CMK in prod."
}

# --- The standardised-photo process (the runner's photos stage, STANDARDISE_PROCESS.md) ---------------------------
variable "standardise_enabled" {
  type        = bool
  default     = false
  description = "SCRAPER_STANDARDISE_AUTO: run the photo process after each produce that published a product sheet."
}

variable "standardise_clean" {
  type        = bool
  default     = false
  description = "SCRAPER_STANDARDISE_CLEAN: the photo process may use the hosted clean-up edits. Off: a listing whose photos all fail for a cleanable reason goes to the review list instead."
}

variable "standardise_budget_usd" {
  type        = string
  default     = ""
  description = "SCRAPER_STANDARDISE_BUDGET_USD: the month's hosted budget (judge + clean-up edits), all runs together. Empty = not configured: the process refuses to start (no default spend)."
}

variable "standardise_job_definition_name" {
  type        = string
  default     = ""
  description = "SCRAPER_STANDARDISE_JOBDEF: the standardise job definition NAME (gpu_enhance module), submitted on gpu_job_queue_name."
}

variable "standardise_job_definition_arn" {
  type        = string
  default     = ""
  description = "Revision-agnostic ARN (':*') of the standardise job definition. Scopes the process's Batch IAM. Empty = not granted."
}

variable "standardise_gpu_vcpus" {
  type        = number
  default     = 8
  description = "SCRAPER_STANDARDISE_GPU_VCPUS: the GPU pool's ceiling while a photo round runs (4 vCPUs per job). Within the account's G-instance quota."
}

variable "gpu_compute_environment_arn" {
  type        = string
  default     = ""
  description = "ARN of the GPU pool behind the queue: the process raises its ceiling for a round and sets it back."
}

# --- Brand + product-type selection (multi-brand / multi-material) ------------
variable "brand" {
  type        = string
  default     = "blokport"
  description = "BLOKPORT_BRAND: the brand this deployment serves; cross-checked against the bucket in prod. (blokport | wudport | calcport | ...)"
}

variable "domain_pack" {
  type        = string
  default     = "stone"
  description = "BLOKPORT_DOMAIN_PACK: the product-domain pack (config/domains/<pack>.yaml) this deployment runs. stone | wood | lime | ... -- the whole product-type model, no code change."
}

variable "sales_channel_id" {
  type        = string
  default     = ""
  description = "BLOKPORT_SALES_CHANNEL_ID: this brand's Medusa sales channel (storefront). One per deployment; empty falls back to the code dev default in dev, required in prod."
}

variable "image_upgrade_batch" {
  description = "Max legacy variant textures re-made on the best model per produce (demand-driven: only variants a product links to). 0 disables the upgrade drip."
  type        = number
  default     = 0
}

variable "run_timeout_seconds" {
  description = "Ceiling on one produce run before the runner kills it as wedged. A healthy full run (live scrape, fresh images, catalog, certify) takes about an hour; 4 h leaves room for a slow day without letting a stuck run hold the lock forever."
  type        = number
  default     = 14400
}

variable "enable_execute_command" {
  description = "Allow ECS Exec (aws ecs execute-command) into the running task. Off by default: it opens an interactive shell into a task that holds the live ledger, so it is enabled per environment, deliberately."
  type        = bool
  default     = false
}
