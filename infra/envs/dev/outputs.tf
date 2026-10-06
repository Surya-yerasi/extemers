output "docqa_url" {
  value = module.docqa_web.function_url
}

output "docqa_ecr_repository_url" {
  value = module.docqa_ecr.repository_url
}

output "docqa_docs_bucket" {
  value = module.docqa_bucket.bucket_name
}

output "docqa_user_pool_id" {
  value = module.docqa_auth.user_pool_id
}
