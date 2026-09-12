terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0" # data.aws_region.region (the .name attribute is deprecated from 6.0)
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.brand # per-brand cost/ownership attribution (default 'blokport' -> unchanged)
      Component = "scraper"
      ManagedBy = "terraform"
    }
  }
}
