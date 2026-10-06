variable "name" {
  type = string
}

variable "keep_last_images" {
  description = "Older images are expired to keep storage cost near zero."
  type        = number
  default     = 15
}
