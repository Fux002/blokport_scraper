# fal.ai key for variant-image generation, stored as a SecureString in SSM.
# OPTIONAL: the scheduled scraper run does NOT use it (it's only for the separate
# variant-image generation), so it's looked up only when fal_key_ssm_name is set.
# This lets the scraper deploy before the key exists. Point the var at the SSM name
# (e.g. /blokport-dev/FAL_KEY) once it's created to inject it into the task.
variable "fal_key_ssm_name" {
  type        = string
  default     = ""
  description = "SSM SecureString name for FAL_KEY. Empty = not injected (scraper doesn't need it)."
}

data "aws_ssm_parameter" "fal_key" {
  count = var.fal_key_ssm_name == "" ? 0 : 1
  name  = var.fal_key_ssm_name
}

module "scraper" {
  source = "./modules/scraper"

  region              = var.region
  home_env            = var.home_env
  dev_staging_bucket  = var.dev_staging_bucket
  prod_staging_bucket = var.prod_staging_bucket
  default_target_env  = var.default_target_env
  image_tag           = var.image_tag
  schedule_enabled    = var.schedule_enabled
  schedule_expression = var.schedule_expression
  keep_scraped        = var.keep_scraped

  # FAL_KEY injected only if configured (see fal_key_ssm_name above).
  ssm_secret_arns = var.fal_key_ssm_name == "" ? {} : { FAL_KEY = data.aws_ssm_parameter.fal_key[0].arn }

  # Sizing — cheapest that runs scrape + pipeline + CPU image enhancement.
  # For de-watermark: image_tag=imageproc + memory=8192 (Florence-2 needs the RAM).
  cpu    = var.cpu
  memory = var.memory
}
