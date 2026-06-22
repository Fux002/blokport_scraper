# Reuses the Medusa platform's state bucket + lock table, under a SEPARATE key.
# ONE state for the one scraper deployment (no per-env states).
terraform {
  backend "s3" {
    bucket         = "blokport-tfstate"
    key            = "blokport/scraper/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "blokport-tflock"
    encrypt        = true
  }
}
