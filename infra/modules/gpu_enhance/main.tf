# =============================================================================
# GPU Batch - AWS Batch, on-demand, scales to ZERO.
# A managed EC2 compute env (min_vcpus=0, so $0 when idle) launches a g4dn GPU only
# while a job runs, then terminates. Its queue runs the variant-texture job (the :gpu
# image, RUN_MODE=generate-textures) and the photo process's standardise jobs (their own
# job definition below). HARD-WIRED to this env's bucket + BLOKPORT_ENV, so dev/prod never cross.
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

# --- IAM: job role (the CONTAINER's app permissions - S3 to THIS bucket only) -
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

# The GPU image is ~4 GB (CUDA base + baked weights). The launch template enlarges the ECS_AL2023_NVIDIA AMI's default 30 GB root volume anyway, as
# safe headroom: docker needs ~2.5x the image size to pull + decompress, and a future baked model would
# otherwise reintroduce the "no space left on device" pull failure that stalls GPU jobs.
resource "aws_launch_template" "this" {
  name = "${local.name}-lt"
  block_device_mappings {
    device_name = "/dev/xvda" # AL2023 ECS AMI root device
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
  # then retire the old one - so config changes (e.g. the launch template) don't deadlock.
  name_prefix = "${local.name}-"
  type        = "MANAGED"
  # service_role omitted -> Batch uses the account service-linked role (AWSServiceRoleForBatch).

  lifecycle {
    create_before_destroy = true
    # The ceiling is set here only when the pool is created: the photo process raises it for a round and sets it back to
    # the idle value (the same variable, passed to it as SCRAPER_STANDARDISE_IDLE_GPU_VCPUS). An apply during a round
    # must not cut the round's burst back to idle under its running jobs.
    ignore_changes = [compute_resources[0].max_vcpus]
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
    # for GPU jobs - without it the container can't see the GPU. AL2023, not AL2:
    # AWS Batch rejects the end-of-life Amazon Linux 2 NVIDIA AMI at CreateComputeEnvironment.
    ec2_configuration {
      image_type = "ECS_AL2023_NVIDIA"
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

# --- Batch: the texture job definition ---------------------------------------
# RUN_MODE=generate-textures -> stone_pipeline.prepare_variant_images (deploy/texture_trigger submits it,
# one job for the whole queue of new-variant textures).
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
      # pack/brand so the texture generation runs THIS material, not stone
      { name = "SCRAPER_BRAND", value = var.brand },
      { name = "SCRAPER_DOMAIN_PACK", value = var.domain_pack },
      { name = "SCRAPER_SALES_CHANNEL_ID", value = var.sales_channel_id },
      { name = "RUN_MODE", value = "generate-textures" },
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

  # Retry ONLY an infrastructure failure: the GPU host went away, or the container never started (nothing ran,
  # so nothing was paid). A job that exits on its own (a partial run, a job that cannot run, a crash) is never
  # retried, because a retry would pay FAL again for what it generated. The job fails once, the failed-job alert
  # below fires once, and the next produce re-queues what did not land. Rules match in order and Batch retries
  # when none matches, so the last rule exits on any reason (test_texture_jobdef pins this order).
  retry_strategy {
    attempts = 2
    evaluate_on_exit {
      action           = "RETRY"
      on_status_reason = "Host EC2*"
    }
    evaluate_on_exit {
      action           = "RETRY"
      on_status_reason = "Task failed to start*"
    }
    evaluate_on_exit {
      action    = "RETRY"
      on_reason = "CannotPullContainerError*"
    }
    evaluate_on_exit {
      action    = "RETRY"
      on_reason = "ResourceInitializationError*"
    }
    evaluate_on_exit {
      action    = "EXIT"
      on_reason = "*"
    }
  }
  timeout {
    # 5h ceiling: one texture job generates the whole queue of new-variant textures; generation is
    # idempotent (a re-run skips what is done), so a generous cap only bounds a stuck job.
    attempt_duration_seconds = 18000
  }
}

# --- Batch: the photo standardiser's job definition ---------------------------
# The standardised-photo process (deploy.standardise_run, on the sync service) submits each GPU slice as a retouch job
# then a standardise job on THIS queue, overriding RUN_MODE, SRC, RUN_TAG, ONLY_KEY and the timeout per submission. Its
# own image (the Dockerfile's standardise targets), pinned per env by its protected tag standardise-pin-<sha>.
# No retries: a slice that fails is retried by the next run, which skips every photo already done. No secrets: every
# model is baked in the image. Created only once an image tag is set.
resource "aws_batch_job_definition" "standardise" {
  count                 = var.standardise_image_tag == "" ? 0 : 1
  name                  = "${var.brand}-standardise-${var.target_env}"
  type                  = "container"
  platform_capabilities = ["EC2"]

  container_properties = jsonencode({
    image            = "${var.image_repo_url}:${var.standardise_image_tag}"
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
      { name = "SCRAPER_BRAND", value = var.brand },
      { name = "SCRAPER_DOMAIN_PACK", value = var.domain_pack },
      { name = "SCRAPER_SALES_CHANNEL_ID", value = var.sales_channel_id },
      { name = "RUN_MODE", value = "standardise" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "standardise"
      }
    }
  })

  timeout {
    attempt_duration_seconds = 3600 # each submission sets its own (STANDARDISE.RETOUCH_TIMEOUT / STANDARDISE_TIMEOUT)
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
    input_template = "\"Auto-texture job <job> (<id>) FAILED in ${local.name}: <reason>. Some or all new-variant images were NOT generated; their products stay HELD until the next produce re-queues them.\""
  }
}
