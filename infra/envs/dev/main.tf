# dev environment root. Removing a module block destroys its resources on the next apply.

data "aws_caller_identity" "current" {}

# Created in infra/bootstrap; looked up by alias so nothing is copied between stacks.
# aws_kms_key (DescribeKey) rather than aws_kms_alias (ListAliases): DescribeKey is
# authorised against the key itself, which the deploy role is already scoped to.
data "aws_kms_key" "docqa" {
  key_id = "alias/docqa"
}

locals {
  docqa_name         = "docqa-dev"
  docqa_config_param = "/docqa/dev/config"
  account_id         = data.aws_caller_identity.current.account_id
  kms_key_arn        = data.aws_kms_key.docqa.arn
  local_base_url     = "http://localhost:8080/" # `make docqa-run` (local container)
  docqa_callback_urls = [
    "${module.docqa_web.function_url}callback",
    "${local.local_base_url}callback",
  ]
}

# ------------------------------------------------------------------ docqa

module "docqa_ecr" {
  providers = { aws = aws.docqa }
  source    = "../../modules/ecr_repo"
  name      = local.docqa_name
}

module "docqa_bucket" {
  providers   = { aws = aws.docqa }
  source      = "../../modules/docs_bucket"
  bucket_name = "${local.docqa_name}-${local.account_id}"
  kms_key_arn = local.kms_key_arn
}

module "docqa_web" {
  providers            = { aws = aws.docqa }
  source               = "../../modules/lambda_container_app"
  name                 = "${local.docqa_name}-web"
  image_uri            = "${module.docqa_ecr.repository_url}:${var.image_tag}"
  kms_key_arn          = local.kms_key_arn
  function_url_enabled = true

  environment_variables = {
    DOCQA_ENVIRONMENT            = "dev"
    DOCQA_CONFIG_PARAMETER       = local.docqa_config_param
    POWERTOOLS_SERVICE_NAME      = "docqa-web"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    AWS_LWA_READINESS_CHECK_PATH = "/health"
  }

  additional_policy_json = data.aws_iam_policy_document.docqa_web.json
}

data "aws_iam_policy_document" "docqa_web" {
  statement {
    sid       = "ReadOwnConfig"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.docqa_config_param}"]
  }
}

module "docqa_auth" {
  providers     = { aws = aws.docqa }
  source        = "../../modules/cognito_auth"
  name          = local.docqa_name
  domain_prefix = "${local.docqa_name}-${local.account_id}"
  callback_urls = local.docqa_callback_urls
  logout_urls   = [module.docqa_web.function_url, local.local_base_url]
}

# Runtime configuration the app reads at cold start. Not secret (IDs and URLs only),
# so a plain String parameter: no KMS decrypt needed by the read-only plan role.
resource "aws_ssm_parameter" "docqa_config" {
  provider = aws.docqa
  name     = local.docqa_config_param
  type     = "String"
  value = jsonencode({
    cognito_domain        = module.docqa_auth.domain_url
    cognito_client_id     = module.docqa_auth.client_id
    cognito_issuer        = module.docqa_auth.issuer
    allowed_redirect_uris = local.docqa_callback_urls
    docs_bucket           = module.docqa_bucket.bucket_name
  })
}

module "docqa_budget" {
  providers         = { aws = aws.docqa }
  source            = "../../modules/budget"
  name              = "${local.docqa_name}-account-monthly"
  monthly_limit_usd = 10
  actual_alert_usd  = 5
  alert_emails      = compact([for e in split(",", var.alert_emails) : trimspace(e)])
}
