variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "state_bucket" {
  type        = string
  default     = "blokport-tfstate"
  description = "S3 bucket holding the Medusa platform's Terraform state (read-only, for the cluster name)."
}

variable "home_env" {
  type        = string
  default     = "dev"
  description = "Which Medusa platform's VPC/cluster HOSTS the task (blokport-<home_env>). The task can still write either staging bucket; this is only where it runs. Default dev."
}

variable "dev_staging_bucket" {
  type        = string
  default     = "blokport-dev-staging-3e58a6"
  description = "Dev staging bucket (the scraper writes improved images here when targeting dev)."
}

variable "prod_staging_bucket" {
  type        = string
  default     = ""
  description = "Prod staging bucket — TBD, fill in when the prod S3 details are shared. Empty = no prod access granted yet."
}

variable "default_target_env" {
  type        = string
  default     = "development"
  description = "The BLOKPORT_ENV baked as the task default (development|production). A run can override it to flip the target bucket at run time."
}

variable "image_tag" {
  type        = string
  default     = "core"
  description = "ECR image tag the task runs (core | imageproc | a git sha)."
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
  description = "Start the default-target cron disabled — run the task manually first, enable when proven."
}

variable "keep_scraped" {
  type        = string
  default     = "true"
  description = "BLOKPORT_KEEP_SCRAPED for the task (keep raw downloads alongside improved)."
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

variable "ssm_secret_arns" {
  type        = map(string)
  default     = {}
  description = "Optional name -> existing SSM SecureString ARN to inject as container secrets (e.g. FAL_KEY). The scheduled run needs none."
}

variable "secrets_kms_key_arn" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "keep_last_images" {
  type    = number
  default = 10
}
