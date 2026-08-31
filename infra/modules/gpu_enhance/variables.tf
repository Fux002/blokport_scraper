variable "target_env" {
  description = "development | production — the env this Batch enhancer belongs to (fixes BLOKPORT_ENV + bucket)."
  type        = string
}

variable "home_env" {
  description = "Which platform VPC/cluster to run in (dev enhancer runs in the dev VPC). Usually == target_env."
  type        = string
}

variable "staging_bucket" {
  description = "The S3 staging bucket for THIS env (scraped/ in, improved/ out). Scoped: the job role can touch only this."
  type        = string
}

variable "image_repo_url" {
  description = "ECR repo URL (shared). The GPU image is <repo>:<image_tag>."
  type        = string
}

variable "image_tag" {
  description = "GPU image tag: ':gpu' for dev (mutable), ':gpu-<sha>' promoted for prod."
  type        = string
  default     = "gpu"
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "instance_types" {
  description = "GPU instance types Batch may launch. g4dn.xlarge (T4) is the cheap default."
  type        = list(string)
  default     = ["g4dn.xlarge"]
}

variable "max_vcpus" {
  description = "Ceiling for the managed compute env. 16 = up to 4 g4dn.xlarge in parallel. min is always 0 ($0 idle)."
  type        = number
  default     = 16
}

variable "root_volume_gb" {
  description = "Instance root EBS size. Must fit the ~19 GB GPU image plus docker's ~2.5x pull/decompress overhead; the 30 GB AMI default is too small. Ephemeral (min=0), so cost is negligible."
  type        = number
  default     = 150
}

variable "ssm_secret_arns" {
  description = "name -> SSM SecureString ARN, injected as container secrets (e.g. proxy for image CDNs)."
  type        = map(string)
  default     = {}
}

variable "secrets_kms_key_arn" {
  description = "KMS key ARN for the SSM SecureStrings (grants kms:Decrypt). Empty = AWS-managed key / none."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alert_email" {
  description = "Ops email for a proactive alert when an auto-texture job FAILS. The texture loop is fire-and-forget with a one-cycle hold, so a failed batch otherwise surfaces only as products missing from Pull. Empty = no alerting resources are created. The SNS email subscription must be CONFIRMED manually (AWS sends a confirmation link on first apply)."
  type        = string
  default     = ""
}

# Brand + product-type selection. The GPU jobs run pipeline code (de-watermark / generate-textures) that
# reads active_pack() from DOMAIN_PACK -- without these, a wudport/wood GPU job would default to the STONE
# pack and generate stone textures/densities. Defaults keep this the blokport-stone stack.
variable "brand" {
  type        = string
  default     = "blokport"
  description = "SCRAPER_BRAND for the GPU job (brand<->bucket guard)."
}

variable "domain_pack" {
  type        = string
  default     = "stone"
  description = "SCRAPER_DOMAIN_PACK -- the product-domain pack the GPU job runs (stone|wood|lime|...)."
}

variable "sales_channel_id" {
  type        = string
  default     = ""
  description = "SCRAPER_SALES_CHANNEL_ID for the GPU job's brand."
}
