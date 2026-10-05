variable "name" {
  description = "Resource name prefix, e.g. calculator-dev."
  type        = string
}

variable "package_path" {
  description = "Path to the Lambda deployment zip."
  type        = string
}

variable "handler" {
  description = "Lambda handler, module.function."
  type        = string
}

variable "runtime" {
  type    = string
  default = "python3.13"
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
  type    = number
  default = 256
}

variable "timeout" {
  description = "Function timeout in seconds. API Gateway caps integrations at 30s."
  type        = number
  default     = 10
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

variable "routes" {
  description = "API Gateway route keys forwarded to the function."
  type        = list(string)
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "throttling_burst_limit" {
  type    = number
  default = 50
}

variable "throttling_rate_limit" {
  type    = number
  default = 100
}

variable "additional_policy_json" {
  description = "Optional IAM policy JSON attached to the execution role (e.g. Bedrock access)."
  type        = string
  default     = null
}
