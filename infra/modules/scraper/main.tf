terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# =============================================================================
# Scraper — ONE environment per module instance (dev OR prod). The root stack
# instantiates this twice. Each instance:
#   * runs in its OWN platform VPC/cluster (home_env: dev runs in blokport-dev,
#     prod in blokport-prod),
#   * is HARD-WIRED to a single target env (BLOKPORT_ENV = target_env, no runtime
#     toggle / no default fallback),
#   * has an IAM task role scoped to ONLY its own staging bucket — so even a
#     misconfigured env var physically cannot write the other environment's bucket.
# The ECR repo + CI deploy role are SHARED and live in the root (one image, built
# once and promoted dev -> prod), passed in as image_repo_url.
# =============================================================================

locals {
  name          = "${var.brand}-scraper-${var.target_env}" # env-suffixed: dev + prod never collide
  platform_name = "${var.brand}-${var.home_env}"           # the VPC/cluster that HOSTS this task

  bucket_arn = "arn:aws:s3:::${var.staging_bucket}"
  object_arn = "arn:aws:s3:::${var.staging_bucket}/*"

  has_secrets = length(var.ssm_secret_arns) > 0
}

data "aws_region" "current" {}

# --- Reuse the platform's network + cluster (read-only) ----------------------
data "aws_vpc" "platform" {
  filter {
    name   = "tag:Name"
    values = ["${local.platform_name}-vpc"]
  }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.platform.id]
  }
  filter {
    name   = "tag:Tier"
    values = ["private"]
  }
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.brand}/${var.home_env}/terraform.tfstate"
    region = var.region
  }
}

data "aws_ecs_cluster" "platform" {
  cluster_name = data.terraform_remote_state.platform.outputs.ecs_cluster_name
}

# --- Logs --------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- Security group: egress only (scrape + S3 + ECR + SSM via NAT) -----------
resource "aws_security_group" "this" {
  name        = "${local.name}-sg"
  description = "Scraper Fargate task (${var.target_env}); egress only"
  vpc_id      = data.aws_vpc.platform.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-sg" }
}

# --- IAM assume-role doc (shared by exec + task roles) -----------------------
data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Execution role (pull image, write logs, read secrets) -------------------
resource "aws_iam_role" "execution" {
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_extra" {
  count = local.has_secrets ? 1 : 0
  statement {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParameters"]
    resources = values(var.ssm_secret_arns)
  }
  dynamic "statement" {
    for_each = var.secrets_kms_key_arn == "" ? [] : [1]
    content {
      sid       = "DecryptSecrets"
      actions   = ["kms:Decrypt"]
      resources = [var.secrets_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "execution_extra" {
  count  = local.has_secrets ? 1 : 0
  name   = "${local.name}-exec-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_extra[0].json
}

# --- Task role: write ONLY this environment's staging bucket ------------------
# Scoped to the single bucket on purpose: a dev task can never write prod (and vice
# versa) even if BLOKPORT_ENV/BLOKPORT_S3_BUCKET were somehow misconfigured -- IAM
# denies it. This is the structural guarantee that the two environments can't mix.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "StagingBucketRW"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [local.object_arn]
  }
  statement {
    sid       = "StagingBucketList"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
  }
  # Auto-enhance: let the scheduled scrape submit the GPU reprocess for newly-staged images. Scoped to
  # ONLY this env's enhance queue + job-definition. Present only when the GPU module is wired (empty = the
  # permission is not granted and the trigger never submits).
  dynamic "statement" {
    for_each = var.gpu_job_queue_arn == "" ? [] : [1]
    content {
      sid     = "SubmitEnhanceJobs"
      actions = ["batch:SubmitJob"]
      # SubmitJob by NAME (what the triggers do) authorizes against the BARE job-definition ARN
      # (.../name), while submit-by-revision authorizes against .../name:<n>. The passed ARN is the ':*'
      # (revision) form, which does NOT match the bare name -> AccessDenied. Allow BOTH so a name-submit works.
      resources = concat([var.gpu_job_queue_arn],
      distinct([var.gpu_job_definition_arn, replace(var.gpu_job_definition_arn, ":*", "")]))
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- ECS task definition: HARD-WIRED to this environment ---------------------
resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "scraper"
    image     = "${var.image_repo_url}:${var.image_tag}"
    essential = true
    # No runtime toggle: BLOKPORT_ENV is fixed to this instance's target env and the
    # bucket is its own. (The prod instance therefore can never default to dev.)
    environment = [
      { name = "SCRAPER_ENV", value = var.target_env },
      { name = "SCRAPER_S3_BUCKET", value = var.staging_bucket },
      { name = "SCRAPER_S3_REGION", value = var.region },
      # Brand + product-type selection. Names are BRAND-NEUTRAL on purpose (the product type is not a brand's
      # property): DOMAIN_PACK is the product-type choice (stone/wood/lime), BRAND names the store (cross-checked
      # against the bucket in prod). Defaults keep this the blokport-stone stack.
      { name = "SCRAPER_BRAND", value = var.brand },
      { name = "SCRAPER_DOMAIN_PACK", value = var.domain_pack },
      { name = "SCRAPER_SALES_CHANNEL_ID", value = var.sales_channel_id },
      { name = "SCRAPER_S3_DRY_RUN", value = "false" },
      { name = "SCRAPER_IMAGE_MODE", value = "s3" },
      { name = "SCRAPER_IMAGE_PROCESSING", value = "true" },
      { name = "SCRAPER_KEEP_SCRAPED", value = var.keep_scraped },
      # Auto-enhance: submit the GPU reprocess for the delta after the scheduled scrape stages new images.
      # Ships OFF (flag false); queue/def names let the trigger reach Batch once enabled. CLASSIFY stays false.
      { name = "SCRAPER_AUTO_ENHANCE", value = tostring(var.auto_enhance_enabled) },
      { name = "SCRAPER_GPU_QUEUE", value = var.gpu_job_queue_name },
      { name = "SCRAPER_GPU_JOBDEF", value = var.gpu_job_definition_name },
      # HARD publish gate: only images the GPU actually enhanced (enhanced/ marker) may be linked. Ships OFF
      # -- flip on ONLY after the markers are backfilled for the already-enhanced set, else every image holds.
      { name = "SCRAPER_REQUIRE_ENHANCED", value = tostring(var.require_enhanced_enabled) },
    ]
    secrets = [for k, v in var.ssm_secret_arns : { name = k, valueFrom = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "scraper"
      }
    }
  }])
}

# --- EventBridge Scheduler -> RunTask ----------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = ["${aws_ecs_task_definition.this.arn_without_revision}:*", aws_ecs_task_definition.this.arn]
  }
  statement {
    sid       = "PassRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "this" {
  name  = "${local.name}-schedule"
  state = var.schedule_enabled ? "ENABLED" : "DISABLED"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = data.aws_ecs_cluster.platform.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.this.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = data.aws_subnets.private.ids
        security_groups  = [aws_security_group.this.id]
        assign_public_ip = false
      }
    }
  }
}
