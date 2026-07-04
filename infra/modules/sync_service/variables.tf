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
