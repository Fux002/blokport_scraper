# --- STD-IMAGE-BUILD (table 348): the standardise GPU image, built on demand on CodeBuild ---------------------------
# A dispatch of .github/workflows/standardise-image.yml (input: a main sha) starts one ephemeral CodeBuild build that
# runs deploy/build_standardise_image.sh at that sha and pushes standardise-pin-<sha12>. One project for the shared
# image repo, so it lives with the repo's owner: gated on create_ecr like the repo itself. New resources only; the
# existing OIDC provider is read (main.tf), the deploy role is NOT reused (it trusts main and can roll ECS).

locals {
  std_build_count    = var.create_ecr ? 1 : 0
  std_build_name     = "standardise-image"
  std_build_log      = "/codebuild/${local.std_build_name}"
  std_build_env      = "standardise-build" # the GitHub environment the dispatch job runs in (main only)
  std_build_log_arn  = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:${local.std_build_log}"
  std_build_proj_arn = "arn:aws:codebuild:${var.region}:${data.aws_caller_identity.current.account_id}:project/${local.std_build_name}"
}

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "std_build" {
  count             = local.std_build_count
  name              = local.std_build_log
  retention_in_days = 14
}

# Role (b): CodeBuild's own, pull the base and push the pin to the one image repo, write its own log group.
data "aws_iam_policy_document" "std_build_service_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    # Confused-deputy guard: only this project, in this account, may hand the role to a build.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.std_build_proj_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "std_build_service" {
  count              = local.std_build_count
  name               = "${var.brand}-standardise-image-build"
  assume_role_policy = data.aws_iam_policy_document.std_build_service_assume.json
}

data "aws_iam_policy_document" "std_build_service" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  # ECR has no tag-level IAM: a pin never moves only because the build script refuses an existing tag (C3).
  statement {
    sid = "EcrPullPushDescribe"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
    ]
    resources = [local.ecr_repo_arn]
  }
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.std_build_log_arn}:*"]
  }
}

resource "aws_iam_role_policy" "std_build_service" {
  count  = local.std_build_count
  name   = "${var.brand}-standardise-image-build"
  role   = aws_iam_role.std_build_service[0].id
  policy = data.aws_iam_policy_document.std_build_service.json
}

resource "aws_codebuild_project" "std_build" {
  count          = local.std_build_count
  name           = local.std_build_name
  description    = "STD-IMAGE-BUILD (table 348): standardise-pin-<sha12> at a main sha, on dispatch only"
  service_role   = aws_iam_role.std_build_service[0].arn
  build_timeout  = 60 # minutes, hard
  queued_timeout = 30

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type            = "LINUX_CONTAINER"
    compute_type    = "BUILD_GENERAL1_LARGE" # 8 vCPU, 16 GiB, 128 GB disk; the precheck proves the fit each build
    image           = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    privileged_mode = true # docker build inside the build
  }

  source {
    type      = "NO_SOURCE"
    buildspec = file("${path.module}/standardise_image/buildspec.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.std_build[0].name
    }
  }
}

# Role (a): the dispatch workflow's. Only a job in the standardise-build environment (deployment branch: main) may
# assume it, and it can only start, read and stop this one project's builds and read their logs and the pushed digest.
data "aws_iam_policy_document" "std_build_dispatch_assume" {
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
      values   = ["repo:${var.github_repo}:environment:${local.std_build_env}"]
    }
  }
}

resource "aws_iam_role" "std_build_dispatch" {
  count              = local.std_build_count
  name               = "${var.brand}-standardise-image-dispatch"
  assume_role_policy = data.aws_iam_policy_document.std_build_dispatch_assume.json
}

data "aws_iam_policy_document" "std_build_dispatch" {
  statement {
    sid       = "OneProject"
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:StopBuild"]
    resources = [local.std_build_proj_arn]
  }
  statement {
    sid       = "ItsLog"
    actions   = ["logs:GetLogEvents"]
    resources = ["${local.std_build_log_arn}:*"]
  }
  statement {
    sid       = "DigestReadBack"
    actions   = ["ecr:DescribeImages"]
    resources = [local.ecr_repo_arn]
  }
}

resource "aws_iam_role_policy" "std_build_dispatch" {
  count  = local.std_build_count
  name   = "${var.brand}-standardise-image-dispatch"
  role   = aws_iam_role.std_build_dispatch[0].id
  policy = data.aws_iam_policy_document.std_build_dispatch.json
}
