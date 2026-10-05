variable "region" {
  type    = string
  default = "us-east-1"
}

variable "owner" {
  type    = string
  default = "surya"
}

variable "package_path" {
  description = "Lambda zip built by scripts/build_lambda.sh."
  type        = string
  default     = "../../../build/lambda.zip"
}
