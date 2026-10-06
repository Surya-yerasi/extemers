variable "name" {
  description = "Function name and prefix for its role and log group, e.g. docqa-dev-web."
  type        = string
}

variable "image_uri" {
  description = "Full ECR image URI including tag."
  type        = string
}

variable "architecture" {
  type    = string
  default = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "memory_size" {
  description = "MB. Also scales CPU proportionally."
  type        = number
  default     = 512
}

variable "timeout" {
  description = "Seconds (max 900)."
  type        = number
  default     = 30
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

variable "kms_key_arn" {
  description = "Customer-managed key used to encrypt the log group."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "image_command" {
  description = "Override the image CMD, e.g. to run a different app from the same image."
  type        = list(string)
  default     = null
}

variable "function_url_enabled" {
  description = "Expose a public HTTPS Function URL (auth type NONE; the app must authenticate)."
  type        = bool
  default     = false
}

variable "additional_policy_json" {
  description = "Extra IAM policy for the function role (S3, Bedrock, SSM, ...)."
  type        = string
  default     = null
}
