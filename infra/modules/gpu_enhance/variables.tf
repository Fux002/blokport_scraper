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
