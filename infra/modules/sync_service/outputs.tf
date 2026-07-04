output "service_sg_id" {
  value       = aws_security_group.this.id
  description = "The service's security group (Medusa->scraper ingress lives here)."
}

output "ledger_efs_id" {
  value       = aws_efs_file_system.ledger.id
  description = "EFS holding the ledger."
}

output "cloud_map_service" {
  value       = aws_service_discovery_service.scraper.name
  description = "Cloud Map service name; internal host is <this>.<namespace>."
}

output "sync_url_hint" {
  value       = "http://${var.service_dns_name}.<namespace>:8723   (Medusa appends /sync/v1)"
  description = "How Medusa reaches sync (root, no path)."
}

output "config_url_hint" {
  value       = "http://${var.service_dns_name}.<namespace>:8724   (Medusa appends /config/v1)"
  description = "How Medusa reaches config (root, no path)."
}
