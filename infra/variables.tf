variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "home_env" {
  type        = string
  default     = "dev"
  description = "Which Medusa platform VPC/cluster hosts the task (blokport-<home_env>). The task can write either staging bucket regardless."
}

variable "dev_staging_bucket" {
  type    = string
  default = "blokport-dev-staging-3e58a6"
}

variable "prod_staging_bucket" {
  type        = string
  default     = ""
  description = "TBD — set once the prod S3 bucket is created/shared. Empty = no prod access granted yet."
}

variable "default_target_env" {
  type        = string
  default     = "development"
  description = "Default BLOKPORT_ENV baked into the task (development|production). Override per run to flip the target."
}

variable "image_tag" {
  type        = string
  default     = "core"
  description = "core = enhancement+upscale; imageproc = also de-watermark (heavier, raise memory)."
}

variable "schedule_enabled" {
  type    = bool
  default = false
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 3 * * ? *)"
}

variable "keep_scraped" {
  type    = string
  default = "true"
}
