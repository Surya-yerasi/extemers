terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial config: bucket comes from backend.hcl (output of infra/bootstrap), so no
  # account-specific values are committed. `terraform init -backend-config=backend.hcl`
  backend "s3" {
    key          = "dev/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "extemers" # umbrella tag: safe to delete everything carrying this; modules add `app`
      env        = "dev"
      owner      = var.owner
      managed_by = "terraform"
      repository = "Surya-yerasi/extemers"
    }
  }
}

# Same provider plus the docqa `app` tag. Every docqa module and resource uses this alias,
# so all of them are tagged app=docqa without per-resource tags blocks.
provider "aws" {
  alias  = "docqa"
  region = var.region

  default_tags {
    tags = {
      project    = "extemers"
      app        = "docqa"
      env        = "dev"
      owner      = var.owner
      managed_by = "terraform"
      repository = "Surya-yerasi/extemers"
    }
  }
}
