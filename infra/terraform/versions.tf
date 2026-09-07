terraform {
  # >= 1.10 for S3 native state locking (use_lockfile). That is what lets this project
  # skip the DynamoDB lock table entirely — one operator, one laptop, one lock file.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The bucket is created by hand, once, before the first `terraform init`:
  #
  #   aws s3 mb s3://aurum-tfstate-851459781998 --region us-east-1
  #   aws s3api put-bucket-versioning --bucket aurum-tfstate-851459781998 \
  #     --versioning-configuration Status=Enabled
  #
  # Two commands beat a whole bootstrap sub-project. Until then,
  # `terraform init -backend=false` validates everything here without touching AWS —
  # which is exactly what CI does.
  backend "s3" {
    bucket       = "aurum-tfstate-851459781998"
    key          = "aurum/terraform.tfstate"
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
