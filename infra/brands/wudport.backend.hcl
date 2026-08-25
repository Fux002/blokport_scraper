# terraform init -reconfigure -backend-config=brands/wudport.backend.hcl
# Requires wudport's own state bucket + lock table to exist first (part of standing up its platform).
bucket         = "wudport-tfstate"
key            = "wudport/scraper/terraform.tfstate"
region         = "eu-west-1"
dynamodb_table = "wudport-tflock"
encrypt        = true
