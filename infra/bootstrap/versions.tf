# One-time, per-account setup. Uses LOCAL state on purpose (it creates the remote
# state bucket). Run manually by an admin; never from CI.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      # Deliberately NOT tagged project=extemers: this stack is account-level
      # plumbing (state bucket, OIDC, IAM roles) that must outlive any single
      # app and must never be caught by a tag-based "delete everything" sweep.
      app        = "extemers-bootstrap"
      managed_by = "terraform"
    }
  }
}
