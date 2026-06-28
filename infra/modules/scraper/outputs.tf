output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "task_role_arn" {
  value       = aws_iam_role.task.arn
  description = "Task role for this env, scoped to its staging bucket only."
}

output "cluster_name" {
  value = data.aws_ecs_cluster.platform.cluster_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
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
