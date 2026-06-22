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

  # Sizing — cheapest that runs scrape + pipeline + CPU image enhancement.
  # Raise memory (~8192) + set image_tag = "imageproc" to enable de-watermark.
  cpu    = 1024
  memory = 4096
}
