terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# =============================================================================
# Scraper — ONE deployment (not a dev/prod pair). It runs as a scheduled Fargate
# task inside the Medusa dev VPC/cluster, but which staging bucket a run writes
# to (dev or prod) is chosen AT RUN TIME via BLOKPORT_ENV + BLOKPORT_S3_BUCKET.
# The task role is granted write to BOTH staging buckets (S3 is regional, not
# VPC-bound), so the same task can target either. The prod bucket is TBD — leave
# prod_staging_bucket empty until its name is shared; dev works in the meantime.
#
# Does NOT alter the Medusa Terraform: it reads the cluster name from that stack's
# remote state and looks up the VPC/subnets/OIDC provider by their names/tags.
# =============================================================================

locals {
  name          = "blokport-scraper"
  platform_name = "blokport-${var.home_env}" # where the task runs (dev)

  # Staging buckets the task may write (prod omitted until its name is set).
  staging_buckets = compact([var.dev_staging_bucket, var.prod_staging_bucket])
  bucket_arns     = [for b in local.staging_buckets : "arn:aws:s3:::${b}"]
  object_arns     = [for b in local.staging_buckets : "arn:aws:s3:::${b}/*"]

  # The bucket baked as the run default (matches default_target_env).
  default_bucket = var.default_target_env == "production" ? var.prod_staging_bucket : var.dev_staging_bucket

  has_secrets = length(var.ssm_secret_arns) > 0
}

data "aws_caller_identity" "current" {}
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
    key    = "blokport/${var.home_env}/terraform.tfstate"
    region = var.region
  }
}

data "aws_ecs_cluster" "platform" {
  cluster_name = data.terraform_remote_state.platform.outputs.ecs_cluster_name
}

# Account-level GitHub OIDC provider created by the Medusa dev stack — reference it.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# --- ECR (one repo) ----------------------------------------------------------
resource "aws_ecr_repository" "this" {
  name                 = local.name
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

# --- Logs --------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- Security group: egress only (scrape + S3 + ECR + SSM via NAT) -----------
resource "aws_security_group" "this" {
  name        = "${local.name}-sg"
  description = "Scraper Fargate task; egress only"
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

# --- Task role (write the dev + prod staging buckets) ------------------------
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "StagingBucketRW"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = local.object_arns
  }
  statement {
    sid       = "StagingBucketList"
    actions   = ["s3:ListBucket"]
    resources = local.bucket_arns
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- ECS task definition (env defaults to the configured target) -------------
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
    image     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
    essential = true
    # Default target = dev. To run against prod, override BLOKPORT_ENV +
    # BLOKPORT_S3_BUCKET at run-task time (see DEPLOY.md), no redeploy needed.
    environment = [
      { name = "BLOKPORT_ENV", value = var.default_target_env },
      { name = "BLOKPORT_S3_BUCKET", value = local.default_bucket },
      { name = "BLOKPORT_S3_REGION", value = var.region },
      { name = "BLOKPORT_S3_DRY_RUN", value = "false" },
      { name = "BLOKPORT_IMAGE_MODE", value = "s3" },
      { name = "BLOKPORT_IMAGE_PROCESSING", value = "true" },
      { name = "BLOKPORT_KEEP_SCRAPED", value = var.keep_scraped },
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

# --- EventBridge Scheduler -> RunTask (default target) -----------------------
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

# --- GitHub OIDC deploy role (CI builds + pushes the image) ------------------
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
  name               = "${local.name}-gha-deploy"
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
  name   = "${local.name}-gha-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
