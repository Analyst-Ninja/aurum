terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Same bucket as the main stack, different key — scripts/deploy.sh creates the bucket
  # if it is missing before this backend is initialised.
  backend "s3" {
    bucket       = "aurum-tfstate-851459781998"
    key          = "aurum/bootstrap.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "aurum"
      ManagedBy = "terraform"
    }
  }
}
