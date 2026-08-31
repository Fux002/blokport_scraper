# terraform init -reconfigure -backend-config=brands/calcport.backend.hcl
# Requires calcport's own state bucket + lock table to exist first (part of standing up its platform).
bucket         = "calcport-tfstate"
key            = "calcport/scraper/terraform.tfstate"
region         = "eu-west-1"
dynamodb_table = "calcport-tflock"
encrypt        = true
