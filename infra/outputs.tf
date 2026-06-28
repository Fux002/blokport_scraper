# --- Shared ---------------------------------------------------------------
output "ecr_repository_url" {
  value       = aws_ecr_repository.this.repository_url
  description = "CI pushes the one image here; both envs pull it (promote the same tag)."
}

output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "Set as the repo secret AWS_DEPLOY_ROLE_ARN for the Deploy workflow."
}

# --- Dev ------------------------------------------------------------------
output "dev_task_definition_family" {
  value = module.scraper_dev.task_definition_family
}

output "dev_cluster_name" {
  value = module.scraper_dev.cluster_name
}

output "dev_schedule_name" {
  value = module.scraper_dev.schedule_name
}

output "dev_private_subnet_ids" {
  value       = module.scraper_dev.private_subnet_ids
  description = "For a manual `aws ecs run-task` of the dev task."
}

output "dev_security_group_id" {
  value = module.scraper_dev.security_group_id
}

# --- Prod (present only once prod_staging_bucket is set) -------------------
output "prod_task_definition_family" {
  value = local.prod_enabled ? module.scraper_prod[0].task_definition_family : null
}

output "prod_cluster_name" {
  value = local.prod_enabled ? module.scraper_prod[0].cluster_name : null
}

output "prod_schedule_name" {
  value = local.prod_enabled ? module.scraper_prod[0].schedule_name : null
}

output "prod_private_subnet_ids" {
  value = local.prod_enabled ? module.scraper_prod[0].private_subnet_ids : null
}

output "prod_security_group_id" {
  value = local.prod_enabled ? module.scraper_prod[0].security_group_id : null
}
