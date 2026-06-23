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

# Residential proxy URL for the Cloudflare-fronted scrapers (varsha + other SlabWare
# tenants). Optional + by-ARN like FAL_KEY: Cloudflare blocks the AWS datacenter IP,
# so those sources need a residential proxy when run from AWS. Locally it's unset.
variable "scraper_proxy_ssm_name" {
  type        = string
  default     = ""
  description = "SSM SecureString name holding the proxy URL (http://user:pass@host:port). Empty = no proxy injected."
}

data "aws_ssm_parameter" "scraper_proxy" {
  count = var.scraper_proxy_ssm_name == "" ? 0 : 1
  name  = var.scraper_proxy_ssm_name
}

locals {
  ssm_secrets = merge(
    var.fal_key_ssm_name == "" ? {} : { FAL_KEY = data.aws_ssm_parameter.fal_key[0].arn },
    var.scraper_proxy_ssm_name == "" ? {} : { BLOKPORT_SCRAPER_PROXY = data.aws_ssm_parameter.scraper_proxy[0].arn },
  )
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

  # Secrets injected only when their SSM names are configured (FAL_KEY, proxy URL).
  ssm_secret_arns = local.ssm_secrets

  # Sizing — cheapest that runs scrape + pipeline + CPU image enhancement.
  # For de-watermark: image_tag=imageproc + memory=8192 (Florence-2 needs the RAM).
  cpu    = var.cpu
  memory = var.memory
}
