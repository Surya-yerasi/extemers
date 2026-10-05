output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "plan_role_arn" {
  description = "GitHub repo variable AWS_PLAN_ROLE_ARN."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "GitHub environment variable AWS_DEPLOY_ROLE_ARN (environment: dev)."
  value       = aws_iam_role.deploy.arn
}
