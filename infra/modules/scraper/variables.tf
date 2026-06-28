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
