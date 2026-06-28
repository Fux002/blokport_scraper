# =============================================================================
# Scraper — TWO deployments from ONE image: a dev task (runs in blokport-dev) and
# a prod task (runs in blokport-prod). Shared here: the ECR repo + the CI deploy
# role (build once, promote the SAME tag dev -> prod). Per-env (in modules/scraper):
# the task def, IAM (scoped to that env's bucket ONLY), schedule, log group.
# The prod instance is created once `prod_staging_bucket` is set.
# =============================================================================

# --- Secrets (optional, by SSM name) ----------------------------------------
# fal.ai key for variant-image generation; the scheduled scrape does NOT need it,
# so it's injected only when fal_key_ssm_name is set.
data "aws_ssm_parameter" "fal_key" {
  count = var.fal_key_ssm_name == "" ? 0 : 1
  name  = var.fal_key_ssm_name
}

# Residential proxy for the Cloudflare-fronted scrapers (optional, by ARN).
data "aws_ssm_parameter" "scraper_proxy" {
  count = var.scraper_proxy_ssm_name == "" ? 0 : 1
  name  = var.scraper_proxy_ssm_name
}

locals {
  ssm_secrets = merge(
    var.fal_key_ssm_name == "" ? {} : { FAL_KEY = data.aws_ssm_parameter.fal_key[0].arn },
    var.scraper_proxy_ssm_name == "" ? {} : { BLOKPORT_SCRAPER_PROXY = data.aws_ssm_parameter.scraper_proxy[0].arn },
  )
  prod_enabled = var.prod_staging_bucket != ""
}

# --- SHARED ECR repository (one image, promoted dev -> prod) ------------------
resource "aws_ecr_repository" "this" {
  name                 = "blokport-scraper"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last ${var.keep_last_images} images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.keep_last_images }
      action       = { type = "expire" }
    }]
  })
}

# --- SHARED GitHub OIDC deploy role (CI builds + pushes the one image) --------
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:${var.github_deploy_ref}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "blokport-scraper-gha-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
    ]
    resources = [aws_ecr_repository.this.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "blokport-scraper-gha-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# --- DEV deployment (runs in blokport-dev, writes the dev bucket only) --------
module "scraper_dev" {
  source = "./modules/scraper"

  target_env     = "development"
  home_env       = "dev"
  staging_bucket = var.dev_staging_bucket
  image_repo_url = aws_ecr_repository.this.repository_url

  region              = var.region
  image_tag           = var.image_tag
  schedule_enabled    = var.dev_schedule_enabled
  schedule_expression = var.schedule_expression
  keep_scraped        = var.keep_scraped
  ssm_secret_arns     = local.ssm_secrets
  cpu                 = var.cpu
  memory              = var.memory
}

# --- PROD deployment (runs in blokport-prod, writes the prod bucket only) -----
# Created only once prod_staging_bucket is set. Its IAM role can write ONLY the
# prod bucket, so it can never touch dev (and dev's role can never touch prod).
module "scraper_prod" {
  source = "./modules/scraper"
  count  = local.prod_enabled ? 1 : 0

  target_env     = "production"
  home_env       = var.prod_home_env
  staging_bucket = var.prod_staging_bucket
  image_repo_url = aws_ecr_repository.this.repository_url

  region              = var.region
  image_tag           = var.prod_image_tag
  schedule_enabled    = var.prod_schedule_enabled
  schedule_expression = var.schedule_expression
  keep_scraped        = var.keep_scraped
  ssm_secret_arns     = local.ssm_secrets
  cpu                 = var.cpu
  memory              = var.memory
}
