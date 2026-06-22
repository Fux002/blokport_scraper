output "ecr_repository_url" {
  value       = aws_ecr_repository.this.repository_url
  description = "Push images here (CI deploy)."
}

output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "cluster_name" {
  value = data.aws_ecs_cluster.platform.cluster_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}

output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "Set as AWS_DEPLOY_ROLE_ARN in the GitHub environment (dev/prod)."
}

output "schedule_name" {
  value = aws_scheduler_schedule.this.name
}

output "private_subnet_ids" {
  value = data.aws_subnets.private.ids
}

output "security_group_id" {
  value = aws_security_group.this.id
}
