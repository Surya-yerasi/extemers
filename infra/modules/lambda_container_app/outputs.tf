output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_arn" {
  value = aws_lambda_function.this.arn
}

output "function_url" {
  description = "HTTPS URL with a trailing slash, or null when disabled."
  value       = var.function_url_enabled ? aws_lambda_function_url.this[0].function_url : null
}

output "role_name" {
  value = aws_iam_role.this.name
}

output "role_arn" {
  value = aws_iam_role.this.arn
}
