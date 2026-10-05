variable "region" {
  type    = string
  default = "us-east-1"
}

variable "github_repository" {
  description = "owner/repo allowed to assume the CI roles."
  type        = string
  default     = "Surya-yerasi/extemers"
}

variable "deploy_environment" {
  description = "GitHub environment whose jobs may assume the deploy role."
  type        = string
  default     = "dev"
}

variable "managed_name_prefixes" {
  description = "Name prefixes of resources the deploy role may manage. Add one per new service."
  type        = list(string)
  default     = ["calculator-"]
}

variable "create_oidc_provider" {
  description = "Set false if the account already has the GitHub OIDC provider."
  type        = bool
  default     = true
}
