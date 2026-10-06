variable "region" {
  type    = string
  default = "us-east-1"
}

variable "owner" {
  type    = string
  default = "surya"
}

variable "image_tag" {
  description = "docqa container image tag in ECR (the git SHA). Set by CI; pass -var image_tag=<sha> locally."
  type        = string
}

variable "alert_emails" {
  description = "Budget alert recipients, comma-separated (e.g. \"a@example.com,b@example.com\"). Empty = no alerts."
  type        = string
  default     = ""
}
