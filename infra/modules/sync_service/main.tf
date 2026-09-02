# The long-running scraper SERVICE (sync + config HTTP servers), in the platform's dev/prod VPC so
# Medusa reaches it VPC-internally over Cloud Map -- NO tunnel. One env per module instance (like the
# batch scraper module). ONE task runs BOTH servers + the local produce subprocess, so all SQLite
# ledger access stays on ONE host. The ledger lives on the task's LOCAL ephemeral volume (shared by
# both containers), NOT EFS/NFS: SQLite over NFS is a reliability footgun and WAL needs local disk
# (design section 12 / M4). Persistence across task restarts is an S3 snapshot the sync server writes
# periodically + on stop, and restores on a cold task (stone_pipeline.ledger.snapshot).
#
# Scope: this module owns ONLY its own resources (task-def, service, its SG + rules, one Cloud Map
# registration in the platform namespace). It never mutates platform-owned resources.

locals {
  name = "${var.brand}-scraper-svc-${var.target_env}"
  tags = { Project = "${var.brand}-scraper", Environment = var.target_env, Component = "sync-service" }
}

data "aws_region" "current" {}

# The ledger is NOT on EFS anymore (see header): it lives on the task's local ephemeral volume and is
# persisted to S3 by the snapshot lane. So there is no EFS file system, mount target, or NFS SG rule.

# --- Security group: egress open; ingress from Medusa on the two ports ---
resource "aws_security_group" "this" {
  name        = "${local.name}-sg"
  description = "Scraper sync/config service (${var.target_env})"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.name}-sg" })
}

# Medusa's tasks -> our two ports (we own our exposure, per the platform side's boundary).
resource "aws_security_group_rule" "sync_from_medusa" {
  type                     = "ingress"
  from_port                = 8723
  to_port                  = 8723
  protocol                 = "tcp"
  security_group_id        = aws_security_group.this.id
  source_security_group_id = var.medusa_service_sg_id
  description              = "Medusa to scraper sync 8723"
}

resource "aws_security_group_rule" "config_from_medusa" {
  type                     = "ingress"
  from_port                = 8724
  to_port                  = 8724
  protocol                 = "tcp"
  security_group_id        = aws_security_group.this.id
  source_security_group_id = var.medusa_service_sg_id
  description              = "Medusa to scraper config 8724"
}

# (No NFS self-rule: the ledger is on the task's local volume, not an EFS mount.)

# --- Logs --------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# --- IAM: execution (pull image, logs, read the token secrets) ---------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadTokens"
    actions   = ["ssm:GetParameters"]
    resources = concat([var.sync_token_ssm_arn, var.config_token_ssm_arn], values(var.produce_secret_arns))
  }
  # A CUSTOMER-managed KMS key (prod's alias/<brand>-prod-secrets) needs an explicit decrypt grant, or the
  # task fails to start (ResourceInitializationError on kms:Decrypt). Empty (dev, the AWS-managed aws/ssm
  # key) omits it -- that key decrypts implicitly via the SSM GetParameters call.
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
  name   = "${local.name}-exec-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- IAM: task role (write ONLY this env's staging bucket; the produce needs it) ---
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "StagingBucketRW"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.staging_bucket}/*"]
  }
  statement {
    sid       = "StagingBucketList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.staging_bucket}"]
  }
  # Auto-enhance: let the produce subprocess submit the GPU reprocess for newly-staged images. Scoped to
  # ONLY this env's enhance queue + job-definition (never any other Batch resource). Present only when the
  # GPU module is wired in (dev); a produce with no queue configured simply never submits (no-op).
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

# --- Task definition: ONE task, TWO containers (sync + config), shared local-disk ledger ---
locals {
  common_env = [
    { name = "SCRAPER_ENV", value = var.target_env },
    { name = "SCRAPER_S3_BUCKET", value = var.staging_bucket },
    { name = "SCRAPER_S3_REGION", value = var.region },
    # Brand + product-type selection (brand-neutral names). Both containers import settings, so both carry
    # BRAND for the prod brand<->bucket guard. DOMAIN_PACK = the product-type choice; defaults keep blokport-stone.
    { name = "SCRAPER_BRAND", value = var.brand },
    { name = "SCRAPER_DOMAIN_PACK", value = var.domain_pack },
    { name = "SCRAPER_SALES_CHANNEL_ID", value = var.sales_channel_id },
    { name = "SCRAPER_LEDGER_PATH", value = "/ledger/${var.target_env}.db" },
    { name = "SCRAPER_LEDGER_WRITETHROUGH", value = "1" },
    { name = "SCRAPER_RUN_MODE", value = "local" },
  ]
  ledger_mount = [{ sourceVolume = "ledger", containerPath = "/ledger", readOnly = false }]
  log_options = {
    "awslogs-group"         = aws_cloudwatch_log_group.this.name
    "awslogs-region"        = data.aws_region.current.name
    "awslogs-stream-prefix" = "svc"
  }
}

resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # Ephemeral task-scoped volume (name only, no efs_volume_configuration): local disk shared by BOTH
  # containers at /ledger. Lost on task stop -- the S3 snapshot lane is what persists the ledger.
  volume {
    name = "ledger"
  }

  container_definitions = jsonencode([
    {
      name      = "sync"
      image     = "${var.image_repo_url}:${var.image_tag}"
      essential = true
      # override the Dockerfile ENTRYPOINT (run_pipeline.sh) -- an empty array is treated as "unset"
      # by ECS. On a cold task the server restores the ledger from its S3 snapshot, else self-seeds a
      # fresh one (snapshot.restore + bootstrap_ledger_if_missing), so no wrapper is needed.
      entryPoint       = ["python", "-m", "stone_pipeline.ledger.server"]
      portMappings     = [{ containerPort = 8723, protocol = "tcp" }]
      environment      = concat(local.common_env, [{ name = "SCRAPER_BIND_HOST", value = "0.0.0.0" }])
      secrets          = [{ name = "SCRAPER_SYNC_TOKEN", valueFrom = var.sync_token_ssm_arn }]
      mountPoints      = local.ledger_mount
      logConfiguration = { logDriver = "awslogs", options = local.log_options }
    },
    {
      name         = "config"
      image        = "${var.image_repo_url}:${var.image_tag}"
      essential    = true
      entryPoint   = ["python", "-m", "stone_pipeline.config.server"]
      portMappings = [{ containerPort = 8724, protocol = "tcp" }]
      # The produce subprocess (fetch -> scrape -> build) runs in THIS container, so its image stage
      # needs s3 mode: download + re-host to improved/ and fill _manifest.json so products carry a
      # linked image and emit. Without it the stage defaults to passthrough, which only reads the
      # manifest and holds every product no_image. On :core (no torch) the enhancement passes the
      # image through un-enhanced; the GPU Batch reprocess (:gpu) de-watermarks/upscales after.
      environment = concat(local.common_env, [
        { name = "SCRAPER_BIND_HOST", value = "0.0.0.0" },
        { name = "SCRAPER_IMAGE_MODE", value = "s3" },
        { name = "SCRAPER_IMAGE_PROCESSING", value = "true" },
        { name = "SCRAPER_KEEP_SCRAPED", value = "true" },
        # s3 dry-run defaults TRUE in dev (a safety so a dev box never writes the bucket): the image
        # stage then derives keys/urls and logs bytes but never put_object's, so processed images never
        # land and every product holds no_image. The produce runs here and MUST write, so turn it off.
        { name = "SCRAPER_S3_DRY_RUN", value = "false" },
        # Auto-enhance: after produce stages new raw images, submit the GPU reprocess for the delta so the
        # GPU spins up on demand and back to zero. Ships OFF (flag false); the queue/job-def names let the
        # trigger reach Batch once enabled. CLASSIFY stays false in the auto path (no uncalibrated discards).
        { name = "SCRAPER_AUTO_ENHANCE", value = tostring(var.auto_enhance_enabled) },
        # Auto-texture: after produce QUEUES new-variant textures, submit ONE GPU job (RUN_MODE=generate-
        # textures) to generate + upload them -- reusing the SAME queue/jobdef below. Ships OFF; flip on only
        # after the :gpu image carries ben2 (else the job fails at background-removal).
        { name = "SCRAPER_AUTO_TEXTURE", value = tostring(var.auto_texture_enabled) },
        { name = "SCRAPER_GPU_QUEUE", value = var.gpu_job_queue_name },
        { name = "SCRAPER_GPU_JOBDEF", value = var.gpu_job_definition_name },
        # HARD publish gate: only GPU-enhanced images (enhanced/ marker) may be linked. Ships OFF -- flip on
        # ONLY after the markers are backfilled for the already-enhanced set, else every image holds.
        { name = "SCRAPER_REQUIRE_ENHANCED", value = tostring(var.require_enhanced_enabled) },
      ])
      # the config container runs the produce subprocess (fetch -> live scrape -> build), so it also
      # carries the scraper's runtime secrets (proxy, fal key) when configured.
      secrets = concat(
        [{ name = "SCRAPER_CONFIG_TOKEN", valueFrom = var.config_token_ssm_arn }],
      [for k, v in var.produce_secret_arns : { name = k, valueFrom = v }])
      mountPoints      = local.ledger_mount
      logConfiguration = { logDriver = "awslogs", options = local.log_options }
    },
  ])

  tags = local.tags
}

# --- Cloud Map: ONE A-record service; both servers are on ONE task (one ENI), so Medusa reaches
# them at the same host on different ports: http://scraper.<ns>:8723 (sync) and :8724 (config).
# (ECS allows one service-registry per service, and this is one task -- one name + two ports is the
# robust shape; two names would force two tasks -> concurrent SQLite writes over NFS. See var.service_dns_name.)
resource "aws_service_discovery_service" "scraper" {
  name = var.service_dns_name
  dns_config {
    namespace_id   = var.internal_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 15
    }
  }
  health_check_custom_config { failure_threshold = 1 }
  tags = local.tags
}

# --- The service -------------------------------------------------------------
resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.this.id]
    assign_public_ip = false
  }

  # A-record + awsvpc: ECS registers the task's ENI IP directly; container_name/port are only for
  # SRV/bridge registries (AWS rejects containerPort here). Medusa reaches both ports at that IP.
  service_registries {
    registry_arn = aws_service_discovery_service.scraper.arn
  }

  depends_on = [aws_service_discovery_service.scraper] # Cloud Map registration exists before the service starts
  tags       = local.tags
}
