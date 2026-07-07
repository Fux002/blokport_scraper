# =============================================================================
# GPU image enhancer — AWS Batch, on-demand, scales to ZERO.
# A managed EC2 compute env (min_vcpus=0, so $0 when idle) launches a g4dn GPU only
# while a job runs, then terminates. The job runs the SAME container image as the
# scraper (:gpu target), reading <env>/products/scraped/ and writing improved/ via
# Real-ESRGAN. HARD-WIRED to this env's bucket + BLOKPORT_ENV, so dev/prod never cross.
# =============================================================================

locals {
  name          = "blokport-gpu-enhance-${var.target_env}"
  platform_name = "blokport-${var.home_env}"
  bucket_arn    = "arn:aws:s3:::${var.staging_bucket}"
  object_arn    = "arn:aws:s3:::${var.staging_bucket}/*"
}

data "aws_region" "current" {}

# --- Reuse the platform network (read-only), same as the scraper module ------
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

resource "aws_cloudwatch_log_group" "this" {
  name              = "/batch/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- Security group: egress only (S3 + ECR + model weights are baked) ---------
resource "aws_security_group" "this" {
  name        = "${local.name}-sg"
  description = "GPU enhancer Batch instances (${var.target_env}); egress only"
  vpc_id      = data.aws_vpc.platform.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-sg" }
}

# --- IAM: EC2 instance role (ECS agent + ECR pull + logs on the GPU host) -----
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_instance" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_instance" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "ecs_instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.ecs_instance.name
}

# --- IAM: execution role (pull the image + ship logs + read the SSM secrets) --
data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Secrets: let the execution role read the injected SSM SecureStrings (+ KMS decrypt).
data "aws_iam_policy_document" "execution_secrets" {
  count = length(var.ssm_secret_arns) == 0 ? 0 : 1
  statement {
    sid       = "ReadSsmSecrets"
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

resource "aws_iam_role_policy" "execution_secrets" {
  count  = length(var.ssm_secret_arns) == 0 ? 0 : 1
  name   = "${local.name}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets[0].json
}

# --- IAM: job role (the CONTAINER's app permissions — S3 to THIS bucket only) -
resource "aws_iam_role" "job" {
  name               = "${local.name}-job"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

data "aws_iam_policy_document" "job" {
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
}

resource "aws_iam_role_policy" "job" {
  name   = "${local.name}-job"
  role   = aws_iam_role.job.id
  policy = data.aws_iam_policy_document.job.json
}

# --- Batch: managed EC2 GPU compute environment (min=0 -> $0 idle) ------------
resource "aws_batch_compute_environment" "this" {
  name = local.name
  type = "MANAGED"
  # service_role omitted -> Batch uses the account service-linked role (AWSServiceRoleForBatch).

  compute_resources {
    type                = "EC2" # On-Demand (Spot G/VT quota is 0 in this account)
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0 # scale to zero when idle
    max_vcpus           = var.max_vcpus
    instance_type       = var.instance_types
    instance_role       = aws_iam_instance_profile.ecs_instance.arn
    security_group_ids  = [aws_security_group.this.id]
    subnets             = data.aws_subnets.private.ids

    # The GPU-optimised ECS AMI (NVIDIA drivers + nvidia-container-runtime). REQUIRED
    # for GPU jobs — without it the container can't see the GPU.
    ec2_configuration {
      image_type = "ECS_AL2_NVIDIA"
    }
    tags = { Name = local.name }
  }
}

resource "aws_batch_job_queue" "this" {
  name     = local.name
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.this.arn
  }
}

# --- Batch: the enhancement job definition -----------------------------------
# RUN_MODE=reprocess -> deploy.reprocess_source reads scraped/<SRC> -> improved/<SRC>
# on the GPU. SRC (and SLICE_*) are overridden per submission for parallel array jobs.
resource "aws_batch_job_definition" "this" {
  name                  = local.name
  type                  = "container"
  platform_capabilities = ["EC2"]

  container_properties = jsonencode({
    image            = "${var.image_repo_url}:${var.image_tag}"
    jobRoleArn       = aws_iam_role.job.arn
    executionRoleArn = aws_iam_role.execution.arn
    resourceRequirements = [
      { type = "GPU", value = "1" },
      { type = "VCPU", value = "4" },
      { type = "MEMORY", value = "15000" },
    ]
    environment = [
      { name = "BLOKPORT_ENV", value = var.target_env },
      { name = "BLOKPORT_S3_BUCKET", value = var.staging_bucket },
      { name = "BLOKPORT_S3_REGION", value = var.region },
      { name = "RUN_MODE", value = "reprocess" },
      { name = "SRC", value = "varsha" }, # per-submission override
      { name = "WATERMARKED", value = "false" },
    ]
    secrets = [for k, v in var.ssm_secret_arns : { name = k, valueFrom = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "gpu-enhance"
      }
    }
  })

  retry_strategy {
    attempts = 2
  }
  timeout {
    attempt_duration_seconds = 7200 # 2h ceiling per job
  }
}
