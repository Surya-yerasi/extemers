variable "name" {
  type = string
}

variable "domain_prefix" {
  description = "Globally unique hosted-login prefix: https://<prefix>.auth.<region>.amazoncognito.com"
  type        = string
}

variable "callback_urls" {
  description = "Exact redirect URIs allowed after login, e.g. https://<function-url>/callback."
  type        = list(string)
}

variable "logout_urls" {
  type = list(string)
}

variable "id_token_validity_minutes" {
  type    = number
  default = 60
}
