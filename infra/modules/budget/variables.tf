variable "name" {
  type = string
}

variable "monthly_limit_usd" {
  type = number
}

variable "actual_alert_usd" {
  description = "Email when actual spend this month passes this amount."
  type        = number
}

variable "alert_emails" {
  description = "Recipients. With an empty list the budget exists but sends no alerts."
  type        = list(string)
  default     = []
}
