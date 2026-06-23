# fal.ai key for variant-image generation, stored as a SecureString in SSM
# (/blokport-<home_env>/FAL_KEY). Looked up by ARN — the value never enters code or
# tfstate; the task decrypts it at runtime via the IAM the module already grants.
data "aws_ssm_parameter" "fal_key" {
  name = "/blokport-${var.home_env}/FAL_KEY"
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

  # Injected into the task as the FAL_KEY env var (container secret) for image generation.
  ssm_secret_arns = { FAL_KEY = data.aws_ssm_parameter.fal_key.arn }

  # Sizing — cheapest that runs scrape + pipeline + CPU image enhancement.
  # Raise memory (~8192) + set image_tag = "imageproc" to enable de-watermark.
  cpu    = 1024
  memory = 4096
}
