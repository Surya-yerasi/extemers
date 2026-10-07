variable "name" {
  description = "Prefix for the dashboard, alarms and topic, e.g. docqa-dev."
  type        = string
}

variable "region" {
  type = string
}

variable "environment" {
  description = "Value of the environment dimension on the app's EMF metrics."
  type        = string
}

variable "metrics_namespace" {
  type    = string
  default = "docqa"
}

variable "web_function_name" {
  type = string
}

variable "ingest_function_name" {
  type = string
}

variable "alert_emails" {
  description = "Alarm recipients (each must confirm the SNS email). Empty: alarms still show on the dashboard."
  type        = list(string)
  default     = []
}

variable "ask_latency_p95_threshold_ms" {
  description = "Alarm when P95 time per question exceeds this over 15 minutes."
  type        = number
  default     = 20000
}
