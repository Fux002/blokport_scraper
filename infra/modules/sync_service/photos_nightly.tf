# --- The overnight photos run: an EventBridge schedule that presses the admin's Run for the photos stage ---------------
# A one-off Fargate task (deploy.start_photos_run) posts {"stage": "photos"} to this service's config API inside the VPC,
# exactly as the button does, so the run is the runner's: one path, the same lock, budget and work window, refused while
# another run is under way, and it gives way to the owner's Publish. The schedule always exists; photos_nightly_enabled
# only switches it on or off, which is one apply that never rolls the service.

locals {
  nightly = "${local.name}-photos-nightly"
}

# Out only: the image pull and logs over the NAT, and the config API (the rule below lets it in on 8724).
resource "aws_security_group" "nightly" {
  name        = "${local.nightly}-sg"
  description = "Overnight photos-run press (${var.target_env})"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.nightly}-sg" })
}

resource "aws_security_group_rule" "config_from_nightly" {
  type                     = "ingress"
  from_port                = 8724
  to_port                  = 8724
  protocol                 = "tcp"
  security_group_id        = aws_security_group.this.id
  source_security_group_id = aws_security_group.nightly.id
  description              = "Overnight photos-run press to scraper config 8724"
}

resource "aws_cloudwatch_log_group" "nightly" {
  name              = "/ecs/${local.nightly}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# Execution only (pull, logs, the config token); the press needs no task role.
resource "aws_iam_role" "nightly_execution" {
  name               = "${local.nightly}-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "nightly_execution_managed" {
  role       = aws_iam_role.nightly_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "nightly_execution_secrets" {
  statement {
    sid       = "ReadConfigToken"
    actions   = ["ssm:GetParameters"]
    resources = [var.config_token_ssm_arn]
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

resource "aws_iam_role_policy" "nightly_execution_secrets" {
  name   = "${local.nightly}-exec-secrets"
  role   = aws_iam_role.nightly_execution.id
  policy = data.aws_iam_policy_document.nightly_execution_secrets.json
}

resource "aws_ecs_task_definition" "nightly" {
  family                   = local.nightly
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.nightly_execution.arn

  container_definitions = jsonencode([
    {
      name        = "press"
      image       = "${var.image_repo_url}:${var.image_tag}"
      essential   = true
      entryPoint  = ["python", "-m", "deploy.start_photos_run"]
      environment = [{ name = "SCRAPER_CONFIG_URL", value = "http://${var.service_dns_name}.${var.internal_namespace_name}:8724" }]
      secrets     = [{ name = "SCRAPER_CONFIG_TOKEN", valueFrom = var.config_token_ssm_arn }]
      logConfiguration = { logDriver = "awslogs", options = {
        "awslogs-group"         = aws_cloudwatch_log_group.nightly.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "press"
      } }
    }
  ])
  tags = local.tags
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "nightly_scheduler" {
  name               = "${local.nightly}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "nightly_scheduler" {
  statement {
    sid       = "RunThePress"
    actions   = ["ecs:RunTask"]
    resources = ["${aws_ecs_task_definition.nightly.arn_without_revision}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.ecs_cluster_arn]
    }
  }
  statement {
    sid       = "PassItsRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.nightly_execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "nightly_scheduler" {
  name   = "${local.nightly}-scheduler"
  role   = aws_iam_role.nightly_scheduler.id
  policy = data.aws_iam_policy_document.nightly_scheduler.json
}

resource "aws_scheduler_schedule" "photos_nightly" {
  name  = local.nightly
  state = var.photos_nightly_enabled ? "ENABLED" : "DISABLED"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression          = var.photos_nightly_schedule
  schedule_expression_timezone = var.photos_nightly_timezone

  target {
    arn      = var.ecs_cluster_arn
    role_arn = aws_iam_role.nightly_scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.nightly.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.nightly.id]
        assign_public_ip = false
      }
    }
    # A press that could not be delivered within the hour is dropped: a late one would start a run into the morning.
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 3
    }
  }
}
