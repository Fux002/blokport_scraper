# The long-running scraper SERVICE (sync + config HTTP servers), in the platform's dev/prod VPC so
# Medusa reaches it VPC-internally over Cloud Map -- NO tunnel. One env per module instance (like the
# batch scraper module). ONE task runs BOTH servers + the local produce subprocess, so all SQLite
# ledger access stays on one host (EFS is persistence only); two tasks would race NFS locks.
#
# Scope: this module owns ONLY its own resources (EFS, task-def, service, its SG + rules, one Cloud
# Map registration in the platform namespace). It never mutates platform-owned resources.

locals {
  name = "blokport-scraper-svc-${var.target_env}"
  tags = { Project = "blokport-scraper", Environment = var.target_env, Component = "sync-service" }
}

data "aws_region" "current" {}

# --- EFS for the ledger (persists across task restarts; single-host access) ---
resource "aws_efs_file_system" "ledger" {
  creation_token = "${local.name}-ledger"
  encrypted      = true
  tags           = merge(local.tags, { Name = "${local.name}-ledger" })
}

resource "aws_efs_mount_target" "ledger" {
  for_each        = toset(var.private_subnet_ids)
  file_system_id  = aws_efs_file_system.ledger.id
  subnet_id       = each.value
  security_groups = [aws_security_group.this.id]
}

# --- Security group: egress open; ingress from Medusa on the two ports + NFS to the EFS mount ---
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

# the task reaches its own EFS mount targets over NFS (same SG on both sides).
resource "aws_security_group_rule" "nfs_self" {
  type              = "ingress"
  from_port         = 2049
  to_port           = 2049
  protocol          = "tcp"
  security_group_id = aws_security_group.this.id
  self              = true
  description       = "NFS to the ledger EFS mount targets"
}

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
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- Task definition: ONE task, TWO containers (sync + config), shared EFS ledger ---
locals {
  common_env = [
    { name = "BLOKPORT_ENV", value = var.target_env },
    { name = "BLOKPORT_S3_BUCKET", value = var.staging_bucket },
    { name = "BLOKPORT_S3_REGION", value = var.region },
    { name = "BLOKPORT_LEDGER_PATH", value = "/ledger/${var.target_env}.db" },
    { name = "BLOKPORT_LEDGER_WRITETHROUGH", value = "1" },
    { name = "BLOKPORT_RUN_MODE", value = "local" },
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

  volume {
    name = "ledger"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.ledger.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
    }
  }

  container_definitions = jsonencode([
    {
      name = "sync"
      image = "${var.image_repo_url}:${var.image_tag}"
      essential = true
      # override the Dockerfile ENTRYPOINT (run_pipeline.sh) -- an empty array is treated as "unset"
      # by ECS. The server self-seeds a missing ledger on a fresh EFS volume (bootstrap_ledger_if_missing),
      # so no wrapper is needed.
      entryPoint   = ["python", "-m", "stone_pipeline.ledger.server"]
      portMappings = [{ containerPort = 8723, protocol = "tcp" }]
      environment  = concat(local.common_env, [{ name = "BLOKPORT_BIND_HOST", value = "0.0.0.0" }])
      secrets      = [{ name = "BLOKPORT_SYNC_TOKEN", valueFrom = var.sync_token_ssm_arn }]
      mountPoints  = local.ledger_mount
      logConfiguration = { logDriver = "awslogs", options = local.log_options }
    },
    {
      name = "config"
      image = "${var.image_repo_url}:${var.image_tag}"
      essential = true
      entryPoint   = ["python", "-m", "stone_pipeline.config.server"]
      portMappings = [{ containerPort = 8724, protocol = "tcp" }]
      environment  = concat(local.common_env, [{ name = "BLOKPORT_BIND_HOST", value = "0.0.0.0" }])
      # the config container runs the produce subprocess (fetch -> live scrape -> build), so it also
      # carries the scraper's runtime secrets (proxy, fal key) when configured.
      secrets = concat(
        [{ name = "BLOKPORT_CONFIG_TOKEN", valueFrom = var.config_token_ssm_arn }],
        [for k, v in var.produce_secret_arns : { name = k, valueFrom = v }])
      mountPoints  = local.ledger_mount
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

  depends_on = [aws_efs_mount_target.ledger]   # EFS + Cloud Map exist before the service starts
  tags       = local.tags
}
