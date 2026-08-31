# =============================================================================
# GPU image enhancer — AWS Batch, on-demand, scales to ZERO.
# A managed EC2 compute env (min_vcpus=0, so $0 when idle) launches a g4dn GPU only
# while a job runs, then terminates. The job runs the SAME container image as the
# scraper (:gpu target), reading <env>/products/scraped/ and writing improved/ via
# Real-ESRGAN. HARD-WIRED to this env's bucket + BLOKPORT_ENV, so dev/prod never cross.
# =============================================================================

locals {
  name          = "${var.brand}-gpu-enhance-${var.target_env}"
  platform_name = "${var.brand}-${var.home_env}"
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

# The GPU image is ~4 GB (CUDA base + baked Real-ESRGAN; de-watermark is the hosted FAL API, no baked
# weights). The launch template enlarges the ECS_AL2_NVIDIA AMI's default 30 GB root volume anyway, as
# safe headroom: docker needs ~2.5x the image size to pull + decompress, and a future baked model would
# otherwise reintroduce the "no space left on device" pull failure that stalls GPU jobs.
resource "aws_launch_template" "this" {
  name = "${local.name}-lt"
  block_device_mappings {
    device_name = "/dev/xvda" # AL2 ECS AMI root device
    ebs {
      volume_size           = var.root_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = local.name }
  }
}

# --- Batch: managed EC2 GPU compute environment (min=0 -> $0 idle) ------------
resource "aws_batch_compute_environment" "this" {
  # name_prefix + create_before_destroy: a compute env referenced by a job queue can't be
  # deleted-then-recreated under the same name (the queue relationship blocks the delete and
  # the name collides). Unique names let Terraform stand up the new env, re-point the queue,
  # then retire the old one — so config changes (e.g. the launch template) don't deadlock.
  name_prefix = "${local.name}-"
  type        = "MANAGED"
  # service_role omitted -> Batch uses the account service-linked role (AWSServiceRoleForBatch).

  lifecycle {
    create_before_destroy = true
  }

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
    # Enlarged root volume (above) so the big GPU image can be pulled.
    launch_template {
      launch_template_id = aws_launch_template.this.id
      version            = aws_launch_template.this.latest_version
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
      { name = "SCRAPER_ENV", value = var.target_env },
      { name = "SCRAPER_S3_BUCKET", value = var.staging_bucket },
      { name = "SCRAPER_S3_REGION", value = var.region },
      # pack/brand so GPU pipeline code (de-watermark, generate-textures) runs THIS material, not stone
      { name = "SCRAPER_BRAND", value = var.brand },
      { name = "SCRAPER_DOMAIN_PACK", value = var.domain_pack },
      { name = "SCRAPER_SALES_CHANNEL_ID", value = var.sales_channel_id },
      { name = "RUN_MODE", value = "reprocess" },
      { name = "SRC", value = "" }, # always overridden per submission (never a hardcoded source)
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
    # 5h ceiling. Per-image cost varies widely by source: enhance-only is ~30s but the de-watermark
    # sources (FAL round-trip on top of ESRGAN) run ~50-65s, so a ~220-image slice can take ~4h. A generous
    # cap is the robust fix (the reprocess is idempotent + on-demand instances are stable) -- fragile
    # per-source slice sizing to fit a tight timeout is what let the slow slices breach the old 2h.
    attempt_duration_seconds = 18000
  }
}

# --- Failure alerting: notify when an auto-texture job FAILS ------------------
# The variant-texture loop is fire-and-forget with a one-cycle hold, so a failed batch surfaces only as
# products missing from Pull. An EventBridge rule on the Batch "FAILED" state change (scoped to THIS queue
# and the "autotexture" job name the produce dispatches) fans out to SNS -> email. Event-driven, no polling.
# All gated on alert_email being set, so an unconfigured env creates nothing.
locals {
  alerts_enabled = var.alert_email == "" ? 0 : 1
}

resource "aws_sns_topic" "alerts" {
  count = local.alerts_enabled
  name  = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = local.alerts_enabled
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "alerts_topic" {
  count = local.alerts_enabled
  statement {
    sid       = "AllowEventBridgePublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts[0].arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  count  = local.alerts_enabled
  arn    = aws_sns_topic.alerts[0].arn
  policy = data.aws_iam_policy_document.alerts_topic[0].json
}

resource "aws_cloudwatch_event_rule" "texture_failed" {
  count       = local.alerts_enabled
  name        = "${local.name}-texture-failed"
  description = "Auto-texture Batch job FAILED in ${local.name}"
  event_pattern = jsonencode({
    source        = ["aws.batch"]
    "detail-type" = ["Batch Job State Change"]
    detail = {
      status   = ["FAILED"]
      jobQueue = [aws_batch_job_queue.this.arn]
      jobName  = [{ prefix = "autotexture" }] # the name the produce's texture dispatch submits
    }
  })
}

resource "aws_cloudwatch_event_target" "texture_failed_sns" {
  count     = local.alerts_enabled
  rule      = aws_cloudwatch_event_rule.texture_failed[0].name
  target_id = "sns"
  arn       = aws_sns_topic.alerts[0].arn
  # a compact human-readable line instead of the raw event JSON
  input_transformer {
    input_paths = {
      job    = "$.detail.jobName"
      id     = "$.detail.jobId"
      reason = "$.detail.statusReason"
    }
    input_template = "\"Auto-texture job <job> (<id>) FAILED in ${local.name}: <reason>. New-variant images were NOT generated; their products stay HELD until a successful re-run.\""
  }
}
