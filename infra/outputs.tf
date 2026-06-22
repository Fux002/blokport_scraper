output "ecr_repository_url" {
  value       = module.scraper.ecr_repository_url
  description = "CI pushes the image here."
}

output "deploy_role_arn" {
  value       = module.scraper.deploy_role_arn
  description = "Set as the repo secret AWS_DEPLOY_ROLE_ARN for the Deploy workflow."
}

output "cluster_name" {
  value = module.scraper.cluster_name
}

output "task_definition_family" {
  value = module.scraper.task_definition_family
}

output "private_subnet_ids" {
  value       = module.scraper.private_subnet_ids
  description = "For `aws ecs run-task` network configuration."
}

output "security_group_id" {
  value = module.scraper.security_group_id
}

output "schedule_name" {
  value = module.scraper.schedule_name
}
