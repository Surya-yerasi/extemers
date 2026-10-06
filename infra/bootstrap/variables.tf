variable "region" {
  type    = string
  default = "us-east-1"
}

variable "github_subject_prefix" {
  description = <<-EOT
    Prefix of the GitHub OIDC token `sub` claim for the repo allowed to assume the CI roles.
    This repo uses GitHub's immutable subject format, repo:OWNER@OWNER_ID/REPO@REPO_ID, which
    stays bound to this exact repo even if it is renamed or a same-named repo is recreated.
    Get the current value with:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = "repo:Surya-yerasi@61959369/extemers@1405205156"

  validation {
    condition     = startswith(var.github_subject_prefix, "repo:")
    error_message = "github_subject_prefix must start with \"repo:\"."
  }
}

variable "deploy_environment" {
  description = "GitHub environment whose jobs may assume the deploy role."
  type        = string
  default     = "dev"
}

variable "managed_name_prefixes" {
  description = "Name prefixes of resources the deploy role may manage. Add one per new service."
  type        = list(string)
  default     = ["docqa-"]
}

variable "create_oidc_provider" {
  description = "Set false if the account already has the GitHub OIDC provider."
  type        = bool
  default     = true
}
