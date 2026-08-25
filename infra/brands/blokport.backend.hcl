# terraform init -reconfigure -backend-config=brands/blokport.backend.hcl
# (matches the default backend.tf; here for uniformity across brands)
bucket         = "blokport-tfstate"
key            = "blokport/scraper/terraform.tfstate"
region         = "eu-west-1"
dynamodb_table = "blokport-tflock"
encrypt        = true
