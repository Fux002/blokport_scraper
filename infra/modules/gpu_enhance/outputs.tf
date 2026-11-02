output "job_queue" {
  description = "Batch job queue name to submit enhancement jobs to."
  value       = aws_batch_job_queue.this.name
}

output "job_queue_arn" {
  value = aws_batch_job_queue.this.arn
}

output "job_definition" {
  description = "Batch job definition name (RUN_MODE=reprocess; override SRC per submission)."
  value       = aws_batch_job_definition.this.name
}

output "job_definition_arn" {
  value = aws_batch_job_definition.this.arn
}

output "compute_environment" {
  value = aws_batch_compute_environment.this.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}

output "compute_environment_arn" {
  description = "The GPU pool the photo process raises the ceiling of for a round (and always sets back)."
  value       = aws_batch_compute_environment.this.arn
}

output "standardise_job_definition" {
  description = "The photo standardiser's job definition NAME (empty when no standardise image tag is set)."
  value       = join("", aws_batch_job_definition.standardise[*].name)
}

output "standardise_job_definition_arn" {
  description = "Its ARN at the current revision (empty when none)."
  value       = join("", aws_batch_job_definition.standardise[*].arn)
}
