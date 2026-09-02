# =============================================================================
# Scraper — TWO deployments from ONE image: a dev task (runs in blokport-dev) and
# a prod task (runs in blokport-prod). Shared here: the ECR repo + the CI deploy
# role (build once, promote the SAME tag dev -> prod). Per-env (in modules/scraper):
# the task def, IAM (scoped to that env's bucket ONLY), schedule, log group.
# The prod instance is created once `prod_staging_bucket` is set.
# =============================================================================

# --- Runtime secrets (FAL key + residential proxy) --------------------------
# DEV: resolved by their KNOWN /${var.brand}-dev/ names, so they are ALWAYS present -- a plain
# `terraform apply` can NEVER strip them from the dev task. This is the durable fix for the recurring
# "FAL_KEY / proxy vanished on apply" drift: dev secrets are wired by convention, exactly like the
# sync/config tokens below, NOT via an optional input var that silently defaults to empty.
data "aws_ssm_parameter" "fal_key_dev" { name = "/${var.brand}-dev/FAL_KEY" }
data "aws_ssm_parameter" "scraper_proxy_dev" { name = "/${var.brand}-dev/BLOKPORT_SCRAPER_PROXY" }

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
  dev_enabled  = var.dev_enabled
  # Shared image repo, resolved from whichever side owns it: the creating brand's resource, or the data
  # source that references it. Every consumer uses these, so create_ecr flips ownership with no other edit.
  ecr_repo_url = var.create_ecr ? aws_ecr_repository.this[0].repository_url : data.aws_ecr_repository.scraper[0].repository_url
  ecr_repo_arn = var.create_ecr ? aws_ecr_repository.this[0].arn : data.aws_ecr_repository.scraper[0].arn
}

# blokport's dev modules pre-date the dev_enabled gate: migrate their state to the count index so adding
# the gate does NOT destroy/recreate the live dev stack (a prod-only brand starts fresh with none of these).
moved {
  from = module.scraper_dev
  to   = module.scraper_dev[0]
}
moved {
  from = module.gpu_enhance_dev
  to   = module.gpu_enhance_dev[0]
}
moved {
  from = module.sync_service_dev
  to   = module.sync_service_dev[0]
}

# --- SHARED ECR repository (one image, promoted dev -> prod) ------------------
# SHARED across brands: the image is brand-agnostic (brand/pack chosen at runtime), so ONE repo serves every
# brand. `create_ecr` decides ownership WITHOUT a code edit: the owning brand (blokport, default true) CREATES
# the repo; every other brand sets create_ecr=false and REFERENCES it via the data source below. All consumers
# read local.ecr_repo_* so they don't care which side supplied it. (Repo name stays "blokport-scraper" -- an
# internal, brand-agnostic image registry name, not a brand-facing string; renaming would orphan every image.)
moved {
  from = aws_ecr_repository.this
  to   = aws_ecr_repository.this[0]
}
moved {
  from = aws_ecr_lifecycle_policy.this
  to   = aws_ecr_lifecycle_policy.this[0]
}
resource "aws_ecr_repository" "this" {
  count                = var.create_ecr ? 1 : 0
  name                 = "blokport-scraper"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  count      = var.create_ecr ? 1 : 0
  repository = aws_ecr_repository.this[0].name
  # ECR evaluates rules by priority and an image matched by a higher rule is NOT re-evaluated by a
  # lower one -- so the prefix rules below PROTECT the semantic tags (core / gpu / imageproc, which can
  # never collide with a hex git-sha) from the broad churn rule. Without this, a single "keep last N,
  # tagStatus=any" rule expired the large, infrequently-rebuilt :gpu image once enough per-commit
  # :<sha> images piled up, silently breaking the GPU Batch (CannotPullImageManifestError).
  policy = jsonencode({
    rules = [
      { rulePriority = 1, description = "keep gpu images (:gpu + gpu-<sha>)",
        selection    = { tagStatus = "tagged", tagPrefixList = ["gpu"], countType = "imageCountMoreThan", countNumber = 3 },
      action = { type = "expire" } },
      { rulePriority = 2, description = "keep imageproc images",
        selection    = { tagStatus = "tagged", tagPrefixList = ["imageproc"], countType = "imageCountMoreThan", countNumber = 2 },
      action = { type = "expire" } },
      { rulePriority = 3, description = "keep :core",
        selection    = { tagStatus = "tagged", tagPrefixList = ["core"], countType = "imageCountMoreThan", countNumber = 2 },
      action = { type = "expire" } },
      { rulePriority = 10, description = "expire the per-commit <sha> churn beyond the last ${var.keep_last_images}",
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.keep_last_images },
      action = { type = "expire" } },
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
  name               = "${var.brand}-scraper-gha-deploy"
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
    resources = [local.ecr_repo_arn]
  }
  # Let the Deploy workflow roll the dev service after a :core push, so a merge actually goes live instead
  # of shipping a new image that ECS never rolls (the mutable-tag gotcha). UpdateService is scoped to ONLY
  # the dev scraper service -- the deploy role can never touch Medusa's own services. DescribeServices is
  # read-only (needed by `aws ecs wait services-stable`) and left unscoped for reliability.
  statement {
    sid       = "EcsRollDevServiceUpdate"
    actions   = ["ecs:UpdateService"]
    resources = ["${replace(data.terraform_remote_state.platform_dev.outputs.ecs_cluster_arn, ":cluster/", ":service/")}/${var.brand}-scraper-svc-development"]
  }
  statement {
    sid       = "EcsDescribeForRollWait"
    actions   = ["ecs:DescribeServices"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.brand}-scraper-gha-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# --- DEV deployment (runs in blokport-dev, writes the dev bucket only) --------
module "scraper_dev" {
  source = "./modules/scraper"

  brand       = var.brand
  domain_pack = var.domain_pack
  count       = local.dev_enabled ? 1 : 0

  target_env     = "development"
  home_env       = "dev"
  staging_bucket = var.dev_staging_bucket
  image_repo_url = local.ecr_repo_url

  region              = var.region
  image_tag           = var.image_tag
  schedule_enabled    = var.dev_schedule_enabled
  schedule_expression = var.schedule_expression
  keep_scraped        = var.keep_scraped
  ssm_secret_arns     = local.dev_ssm_secrets
  cpu                 = var.cpu
  memory              = var.memory

  # Auto-enhance: the scheduled scrape submits the dev GPU reprocess for newly-staged images. ON in dev
  # (dev_auto_enhance defaults true). Prod stays unwired until its own GPU module is active.
  gpu_job_queue_name       = module.gpu_enhance_dev[0].job_queue
  gpu_job_definition_name  = module.gpu_enhance_dev[0].job_definition
  gpu_job_queue_arn        = module.gpu_enhance_dev[0].job_queue_arn
  gpu_job_definition_arn   = local.dev_gpu_jobdef_iam_arn # revision-agnostic (see locals): survives job-def bumps
  auto_enhance_enabled     = var.dev_auto_enhance
  require_enhanced_enabled = var.dev_require_enhanced
}

# --- PROD deployment (runs in blokport-prod, writes the prod bucket only) -----
# Created only once prod_staging_bucket is set. Its IAM role can write ONLY the
# prod bucket, so it can never touch dev (and dev's role can never touch prod).
module "scraper_prod" {
  source = "./modules/scraper"

  brand            = var.brand
  domain_pack      = var.domain_pack
  sales_channel_id = var.prod_sales_channel_id
  count            = local.prod_enabled ? 1 : 0

  target_env     = "production"
  home_env       = var.prod_home_env
  staging_bucket = var.prod_staging_bucket
  image_repo_url = local.ecr_repo_url

  region              = var.region
  image_tag           = var.prod_image_tag
  schedule_enabled    = var.prod_schedule_enabled
  schedule_expression = var.schedule_expression
  keep_scraped        = var.keep_scraped
  ssm_secret_arns     = local.prod_ssm_secrets
  secrets_kms_key_arn = local.prod_secrets_kms_key_arn
  cpu                 = var.cpu
  memory              = var.memory
}

# =============================================================================
# GPU image enhancer (AWS Batch, on-demand, scales to 0). One per env, scoped to
# that env's bucket. Runs the :gpu image (Real-ESRGAN enhance + FAL FLUX Fill de-watermark).
# Dev tracks :gpu; prod uses the promoted :gpu-<sha>. FAL_KEY is injected on dev (de-watermark).
# =============================================================================
module "gpu_enhance_dev" {
  source = "./modules/gpu_enhance"

  brand       = var.brand
  domain_pack = var.domain_pack
  count       = local.dev_enabled ? 1 : 0

  target_env     = "development"
  home_env       = "dev"
  staging_bucket = var.dev_staging_bucket
  image_repo_url = local.ecr_repo_url
  image_tag      = var.gpu_image_tag
  region         = var.region
  # 128 vCPU = up to 32 g4dn.xlarge in parallel. Full-catalog reprocess is ~53 GPU-hours at ~30s/image;
  # 4 GPUs (the default 16) took >2h per slice and hit the Batch timeout. min stays 0, so idle cost is $0
  # and total spend is ~flat vs 4 GPUs (same GPU-hours) -- this only compresses wall-clock to ~1.5-2h.
  max_vcpus = 128
  # FAL_KEY for the FAL FLUX Fill de-watermarker (watermarked sources). Wired by convention to the known
  # dev SSM param, exactly like the scraper/produce secrets, so a plain apply can never strip it.
  ssm_secret_arns = { FAL_KEY = data.aws_ssm_parameter.fal_key_dev.arn }
  alert_email     = var.alert_email
}

locals {
  # The batch:SubmitJob IAM for the auto-enhance triggers must survive job-definition REVISION bumps. The
  # trigger submits by NAME, which resolves to the latest revision; pinning the policy to the exact :N ARN
  # (module output) means any job-def change (e.g. adding a secret) silently breaks the submit with an IAM
  # denial until every dependent module is re-applied. Match all revisions by NAME instead: strip the
  # trailing :<revision> and allow :*. Queue ARNs have no revision, so they are used as-is.
  dev_gpu_jobdef_iam_arn = local.dev_enabled ? replace(module.gpu_enhance_dev[0].job_definition_arn, "/:[0-9]+$/", ":*") : ""
}

module "gpu_enhance_prod" {
  source = "./modules/gpu_enhance"

  brand            = var.brand
  domain_pack      = var.domain_pack
  sales_channel_id = var.prod_sales_channel_id
  count            = local.prod_enabled ? 1 : 0

  target_env     = "production"
  home_env       = var.prod_home_env
  staging_bucket = var.prod_staging_bucket
  image_repo_url = local.ecr_repo_url
  image_tag      = var.prod_gpu_image_tag
  region         = var.region
  alert_email    = var.alert_email
  # FAL_KEY (+ proxy) for FLUX texture gen + FAL de-watermark, by convention like dev. Empty until the
  # prod SSM params are configured (local.prod_ssm_secrets), so a plain apply never strips or invents it.
  ssm_secret_arns     = local.prod_ssm_secrets
  secrets_kms_key_arn = local.prod_secrets_kms_key_arn
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
    bucket = var.platform_state_bucket
    key    = "${var.brand}/dev/terraform.tfstate"
    region = var.region
  }
}

data "aws_ssm_parameter" "sync_token_dev" { name = "/${var.brand}-dev/BLOKPORT_SYNC_TOKEN" }
data "aws_ssm_parameter" "config_token_dev" { name = "/${var.brand}-dev/BLOKPORT_CONFIG_TOKEN" }

# Reference the EXISTING shared ECR by name (data source, not the root resource) so this module can
# apply with -target without depending on the root ECR -- which is currently drifted from state
# (state has module.scraper.aws_ecr_repository; the repo config was refactored but never applied).
# Referenced ONLY by a non-owning brand (create_ecr=false); the owning brand creates the repo above instead.
data "aws_ecr_repository" "scraper" {
  count = var.create_ecr ? 0 : 1
  name  = "blokport-scraper"
}

module "sync_service_dev" {
  source = "./modules/sync_service"

  brand       = var.brand
  domain_pack = var.domain_pack
  count       = local.dev_enabled ? 1 : 0

  target_env     = "development"
  image_repo_url = local.ecr_repo_url
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
  # queue/def names). ON in dev (dev_auto_enhance defaults true). Auto-texture reuses the same queue/jobdef.
  gpu_job_queue_name       = module.gpu_enhance_dev[0].job_queue
  gpu_job_definition_name  = module.gpu_enhance_dev[0].job_definition
  gpu_job_queue_arn        = module.gpu_enhance_dev[0].job_queue_arn
  gpu_job_definition_arn   = local.dev_gpu_jobdef_iam_arn # revision-agnostic (see locals): survives job-def bumps
  auto_enhance_enabled     = var.dev_auto_enhance
  auto_texture_enabled     = var.dev_auto_texture
  require_enhanced_enabled = var.dev_require_enhanced
}

# =============================================================================
# In-VPC sync/config SERVICE (PROD) -- the mirror of the dev service, count-gated on prod_staging_bucket
# so it is INERT until prod is provisioned: with prod disabled the prod platform remote state and the
# /${var.brand}-prod/ tokens are never read, so a dev apply is unaffected. It runs in Blokport's PROD platform
# VPC over Cloud Map, so before enabling it the prod platform stack must exist (blokport/prod/terraform.tfstate)
# and the prod SSM tokens + FAL_KEY must be created (see TODO_PROD.md, section 1).
# =============================================================================
data "terraform_remote_state" "platform_prod" {
  count   = local.prod_enabled ? 1 : 0
  backend = "s3"
  config = {
    bucket = var.platform_state_bucket
    key    = "${var.brand}/prod/terraform.tfstate"
    region = var.region
  }
}

data "aws_ssm_parameter" "sync_token_prod" {
  count = local.prod_enabled ? 1 : 0
  name  = "/${var.brand}-prod/BLOKPORT_SYNC_TOKEN"
}

data "aws_ssm_parameter" "config_token_prod" {
  count = local.prod_enabled ? 1 : 0
  name  = "/${var.brand}-prod/BLOKPORT_CONFIG_TOKEN"
}

# The prod SSM SecureStrings are encrypted with the brand's CUSTOMER-managed CMK (alias/<brand>-prod-secrets);
# the task execution roles need kms:Decrypt on it or ECS fails to pull the secrets. Dev uses the AWS-managed
# aws/ssm key (implicit decrypt), so this is prod-only. The key policy delegates to account IAM, so granting
# decrypt on the execution role (via secrets_kms_key_arn below) is sufficient -- no key-policy change needed.
data "aws_kms_key" "prod_secrets" {
  count  = local.prod_enabled ? 1 : 0
  key_id = "alias/${var.brand}-prod-secrets"
}

locals {
  prod_secrets_kms_key_arn = local.prod_enabled ? data.aws_kms_key.prod_secrets[0].arn : ""
  # Revision-agnostic prod job-def ARN for the auto-enhance/texture SubmitJob IAM (see dev_gpu_jobdef_iam_arn).
  # Guarded by prod_enabled so the count-gated module index [0] is only reached when prod exists.
  prod_gpu_jobdef_iam_arn = local.prod_enabled ? replace(module.gpu_enhance_prod[0].job_definition_arn, "/:[0-9]+$/", ":*") : ""
}

module "sync_service_prod" {
  source = "./modules/sync_service"

  brand            = var.brand
  domain_pack      = var.domain_pack
  sales_channel_id = var.prod_sales_channel_id
  count            = local.prod_enabled ? 1 : 0

  target_env     = "production"
  image_repo_url = local.ecr_repo_url
  image_tag      = var.prod_image_tag
  region         = var.region
  staging_bucket = var.prod_staging_bucket
  memory         = 8192 # same catalog RAM peak as dev (produce ~1.5 GB + servers ~0.5 GB)

  vpc_id                = data.terraform_remote_state.platform_prod[0].outputs.vpc_id
  private_subnet_ids    = data.terraform_remote_state.platform_prod[0].outputs.private_subnet_ids
  ecs_cluster_arn       = data.terraform_remote_state.platform_prod[0].outputs.ecs_cluster_arn
  medusa_service_sg_id  = data.terraform_remote_state.platform_prod[0].outputs.service_sg_id
  internal_namespace_id = data.terraform_remote_state.platform_prod[0].outputs.internal_namespace_id

  sync_token_ssm_arn   = data.aws_ssm_parameter.sync_token_prod[0].arn
  config_token_ssm_arn = data.aws_ssm_parameter.config_token_prod[0].arn
  produce_secret_arns  = local.prod_ssm_secrets
  secrets_kms_key_arn  = local.prod_secrets_kms_key_arn

  gpu_job_queue_name       = module.gpu_enhance_prod[0].job_queue
  gpu_job_definition_name  = module.gpu_enhance_prod[0].job_definition
  gpu_job_queue_arn        = module.gpu_enhance_prod[0].job_queue_arn
  gpu_job_definition_arn   = local.prod_gpu_jobdef_iam_arn
  auto_enhance_enabled     = var.prod_auto_enhance
  auto_texture_enabled     = var.prod_auto_texture
  require_enhanced_enabled = var.prod_require_enhanced
}
