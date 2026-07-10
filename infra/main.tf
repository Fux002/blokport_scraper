# =============================================================================
# Scraper — TWO deployments from ONE image: a dev task (runs in blokport-dev) and
# a prod task (runs in blokport-prod). Shared here: the ECR repo + the CI deploy
# role (build once, promote the SAME tag dev -> prod). Per-env (in modules/scraper):
# the task def, IAM (scoped to that env's bucket ONLY), schedule, log group.
# The prod instance is created once `prod_staging_bucket` is set.
# =============================================================================

# --- Runtime secrets (FAL key + residential proxy) --------------------------
# DEV: resolved by their KNOWN /blokport-dev/ names, so they are ALWAYS present -- a plain
# `terraform apply` can NEVER strip them from the dev task. This is the durable fix for the recurring
# "FAL_KEY / proxy vanished on apply" drift: dev secrets are wired by convention, exactly like the
# sync/config tokens below, NOT via an optional input var that silently defaults to empty.
data "aws_ssm_parameter" "fal_key_dev" { name = "/blokport-dev/FAL_KEY" }
data "aws_ssm_parameter" "scraper_proxy_dev" { name = "/blokport-dev/BLOKPORT_SCRAPER_PROXY" }

# PROD: still by explicit SSM-name var (empty until the prod params exist), so a dev deploy never forces
# prod secret wiring. Consumed ONLY by module.scraper_prod (count-gated on prod_staging_bucket).
data "aws_ssm_parameter" "fal_key" {
  count = var.fal_key_ssm_name == "" ? 0 : 1
  name  = var.fal_key_ssm_name
}
data "aws_ssm_parameter" "scraper_proxy" {
  count = var.scraper_proxy_ssm_name == "" ? 0 : 1
  name  = var.scraper_proxy_ssm_name
}

locals {
  # DEV runtime secrets, ALWAYS injected (the two data sources above always resolve).
  dev_ssm_secrets = {
    FAL_KEY                = data.aws_ssm_parameter.fal_key_dev.arn
    BLOKPORT_SCRAPER_PROXY = data.aws_ssm_parameter.scraper_proxy_dev.arn
  }
  # PROD runtime secrets, by explicit var (empty until the prod SSM params are configured).
  prod_ssm_secrets = merge(
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
  # ECR evaluates rules by priority and an image matched by a higher rule is NOT re-evaluated by a
  # lower one -- so the prefix rules below PROTECT the semantic tags (core / gpu / imageproc, which can
  # never collide with a hex git-sha) from the broad churn rule. Without this, a single "keep last N,
  # tagStatus=any" rule expired the large, infrequently-rebuilt :gpu image once enough per-commit
  # :<sha> images piled up, silently breaking the GPU Batch (CannotPullImageManifestError).
  policy = jsonencode({
    rules = [
      { rulePriority = 1, description = "keep gpu images (:gpu + gpu-<sha>)",
        selection = { tagStatus = "tagged", tagPrefixList = ["gpu"], countType = "imageCountMoreThan", countNumber = 3 },
        action    = { type = "expire" } },
      { rulePriority = 2, description = "keep imageproc images",
        selection = { tagStatus = "tagged", tagPrefixList = ["imageproc"], countType = "imageCountMoreThan", countNumber = 2 },
        action    = { type = "expire" } },
      { rulePriority = 3, description = "keep :core",
        selection = { tagStatus = "tagged", tagPrefixList = ["core"], countType = "imageCountMoreThan", countNumber = 2 },
        action    = { type = "expire" } },
      { rulePriority = 10, description = "expire the per-commit <sha> churn beyond the last ${var.keep_last_images}",
        selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.keep_last_images },
        action    = { type = "expire" } },
    ]
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
  # Let the Deploy workflow roll the dev service after a :core push, so a merge actually goes live instead
  # of shipping a new image that ECS never rolls (the mutable-tag gotcha). UpdateService is scoped to ONLY
  # the dev scraper service -- the deploy role can never touch Medusa's own services. DescribeServices is
  # read-only (needed by `aws ecs wait services-stable`) and left unscoped for reliability.
  statement {
    sid       = "EcsRollDevServiceUpdate"
    actions   = ["ecs:UpdateService"]
    resources = ["${replace(data.terraform_remote_state.platform_dev.outputs.ecs_cluster_arn, ":cluster/", ":service/")}/blokport-scraper-svc-development"]
  }
  statement {
    sid       = "EcsDescribeForRollWait"
    actions   = ["ecs:DescribeServices"]
    resources = ["*"]
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
  ssm_secret_arns     = local.dev_ssm_secrets
  cpu                 = var.cpu
  memory              = var.memory

  # Auto-enhance: the scheduled scrape submits the dev GPU reprocess for newly-staged images. Ships OFF
  # (dev_auto_enhance defaults false). Prod stays unwired until its own GPU module is active.
  gpu_job_queue_name      = module.gpu_enhance_dev.job_queue
  gpu_job_definition_name = module.gpu_enhance_dev.job_definition
  gpu_job_queue_arn       = module.gpu_enhance_dev.job_queue_arn
  gpu_job_definition_arn  = module.gpu_enhance_dev.job_definition_arn
  auto_enhance_enabled    = var.dev_auto_enhance
  require_enhanced_enabled = var.dev_require_enhanced
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
  ssm_secret_arns     = local.prod_ssm_secrets
  cpu                 = var.cpu
  memory              = var.memory
}

# =============================================================================
# GPU image enhancer (AWS Batch, on-demand, scales to 0). One per env, scoped to
# that env's bucket. Runs the :gpu image (Real-ESRGAN). Dev tracks :gpu; prod uses
# the promoted :gpu-<sha>. Only the CDN proxy secret is injected (no FAL_KEY needed).
# =============================================================================
module "gpu_enhance_dev" {
  source = "./modules/gpu_enhance"

  target_env     = "development"
  home_env       = "dev"
  staging_bucket = var.dev_staging_bucket
  image_repo_url = data.aws_ecr_repository.scraper.repository_url # shared repo (data source; decoupled from ECR refactor)
  image_tag      = var.gpu_image_tag
  region         = var.region
  # 128 vCPU = up to 32 g4dn.xlarge in parallel. Full-catalog reprocess is ~53 GPU-hours at ~30s/image;
  # 4 GPUs (the default 16) took >2h per slice and hit the Batch timeout. min stays 0, so idle cost is $0
  # and total spend is ~flat vs 4 GPUs (same GPU-hours) -- this only compresses wall-clock to ~1.5-2h.
  max_vcpus      = 128
}

module "gpu_enhance_prod" {
  source = "./modules/gpu_enhance"
  count  = local.prod_enabled ? 1 : 0

  target_env     = "production"
  home_env       = var.prod_home_env
  staging_bucket = var.prod_staging_bucket
  image_repo_url = data.aws_ecr_repository.scraper.repository_url
  image_tag      = var.prod_gpu_image_tag
  region         = var.region
}

# =============================================================================
# In-VPC sync/config SERVICE (dev) -- the long-running sync + config HTTP servers
# run in the blokport-dev cluster so Medusa reaches them over Cloud Map (private
# DNS), NOT a Cloudflare tunnel. One task (both servers + local produce), ledger on
# EFS. Consumes the Medusa platform's remote-state outputs (published live to state).
# =============================================================================
data "terraform_remote_state" "platform_dev" {
  backend = "s3"
  config = {
    bucket = "blokport-tfstate"
    key    = "blokport/dev/terraform.tfstate"
    region = var.region
  }
}

data "aws_ssm_parameter" "sync_token_dev" { name = "/blokport-dev/BLOKPORT_SYNC_TOKEN" }
data "aws_ssm_parameter" "config_token_dev" { name = "/blokport-dev/BLOKPORT_CONFIG_TOKEN" }

# Reference the EXISTING shared ECR by name (data source, not the root resource) so this module can
# apply with -target without depending on the root ECR -- which is currently drifted from state
# (state has module.scraper.aws_ecr_repository; the repo config was refactored but never applied).
data "aws_ecr_repository" "scraper" { name = "blokport-scraper" }

module "sync_service_dev" {
  source = "./modules/sync_service"

  target_env     = "development"
  image_repo_url = data.aws_ecr_repository.scraper.repository_url
  # DEV tracks :core (= what's on main); the branch is merged. :core = bbf35a2 (WAL + local-disk ledger).
  image_tag      = var.image_tag
  region         = var.region
  staging_bucket = var.dev_staging_bucket
  # The produce subprocess builds ~2M combinations in RAM (the catalog peak) sharing the task with BOTH
  # servers -- 2 GB OOM-killed it (exit -9). tree_build no longer duplicates that 2M-row set (a2433dd),
  # dropping the peak by a full copy, so 8 GB is comfortable (produce ~1.5 GB + servers ~0.5 GB) at 1
  # vCPU (servers idle; only the occasional produce needs CPU). Can right-size lower once a real /run's
  # peak is measured. NOT a separate produce task -- that races the single-host SQLite ledger invariant.
  memory = 8192

  vpc_id                = data.terraform_remote_state.platform_dev.outputs.vpc_id
  private_subnet_ids    = data.terraform_remote_state.platform_dev.outputs.private_subnet_ids
  ecs_cluster_arn       = data.terraform_remote_state.platform_dev.outputs.ecs_cluster_arn
  medusa_service_sg_id  = data.terraform_remote_state.platform_dev.outputs.service_sg_id
  internal_namespace_id = data.terraform_remote_state.platform_dev.outputs.internal_namespace_id

  sync_token_ssm_arn   = data.aws_ssm_parameter.sync_token_dev.arn
  config_token_ssm_arn = data.aws_ssm_parameter.config_token_dev.arn
  # the produce subprocess (live scrape + image gen) carries the same runtime secrets as the batch
  # scraper -- proxy for the Cloudflare-fronted sites, fal key for images -- when they're configured.
  produce_secret_arns = local.dev_ssm_secrets

  # Auto-enhance: the produce trigger submits the dev GPU reprocess for newly-staged images (scoped IAM +
  # queue/def names). Ships OFF (dev_auto_enhance defaults false); flip the var to enable after testing.
  gpu_job_queue_name      = module.gpu_enhance_dev.job_queue
  gpu_job_definition_name = module.gpu_enhance_dev.job_definition
  gpu_job_queue_arn       = module.gpu_enhance_dev.job_queue_arn
  gpu_job_definition_arn  = module.gpu_enhance_dev.job_definition_arn
  auto_enhance_enabled    = var.dev_auto_enhance
  require_enhanced_enabled = var.dev_require_enhanced
}
