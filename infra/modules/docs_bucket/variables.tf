variable "bucket_name" {
  type = string
}

variable "kms_key_arn" {
  description = "Customer-managed key for SSE-KMS default encryption."
  type        = string
}

variable "noncurrent_version_expiration_days" {
  description = "How long overwritten/deleted object versions are kept for recovery."
  type        = number
  default     = 30
}
