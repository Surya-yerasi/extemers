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
  memory_size          = 1024 # LanceDB search + pyarrow; more memory also means more CPU
  timeout              = 60   # embed + search + rerank + generate, with cold start headroom

  environment_variables = {
    DOCQA_ENVIRONMENT            = "dev"
    DOCQA_CONFIG_PARAMETER       = local.docqa_config_param
    POWERTOOLS_SERVICE_NAME      = "docqa-web"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    AWS_LWA_READINESS_CHECK_PATH = "/health"
  }

  additional_policy_json = data.aws_iam_policy_document.docqa_web.json
}

# The web function answers questions: read-only on the index, no access to raw/ or parsed/.
data "aws_iam_policy_document" "docqa_web" {
  statement {
    sid       = "ReadOwnConfig"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.docqa_config_param}"]
  }

  statement {
    sid       = "ReadIndex"
    actions   = ["s3:GetObject"]
    resources = ["${module.docqa_bucket.bucket_arn}/lancedb/*"]
  }

  statement {
    sid       = "ListIndex"
    actions   = ["s3:ListBucket"]
    resources = [module.docqa_bucket.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["lancedb/*", "lancedb"]
    }
  }

  statement {
    sid       = "DecryptIndex"
    actions   = ["kms:Decrypt"]
    resources = [local.kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid     = "InvokeModels"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [for p in local.web_bedrock_profiles : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${p}"],
      [for p in local.web_bedrock_profiles : "arn:aws:bedrock:*::foundation-model/${trimprefix(p, "us.")}"],
      [for m in local.web_bedrock_models : "arn:aws:bedrock:${var.region}::foundation-model/${m}"],
    )
  }
}

# Ingestion: same image, different app (docqa.ingest_app). S3 events arrive through the
# Lambda Web Adapter as POST /events; no Function URL, so /events is not reachable publicly.
module "docqa_ingest" {
  providers   = { aws = aws.docqa }
  source      = "../../modules/lambda_container_app"
  name        = "${local.docqa_name}-ingest"
  image_uri   = "${module.docqa_ecr.repository_url}:${var.image_tag}"
  kms_key_arn = local.kms_key_arn
  memory_size = 2048 # PDF rendering + LanceDB writes
  timeout     = 900

  image_command = [
    "uvicorn", "docqa.ingest_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080",
  ]

  environment_variables = {
    DOCQA_DOCS_BUCKET            = module.docqa_bucket.bucket_name
    POWERTOOLS_SERVICE_NAME      = "docqa-ingest"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    AWS_LWA_READINESS_CHECK_PATH = "/health"
    AWS_LWA_PASS_THROUGH_PATH    = "/events"
    AWS_LWA_ERROR_STATUS_CODES   = "500-599" # failed ingest => failed invocation => S3 retry
  }

  additional_policy_json = data.aws_iam_policy_document.docqa_ingest.json
}

locals {
  bedrock_profiles     = ["us.amazon.nova-2-lite-v1:0", "us.amazon.nova-micro-v1:0"]
  web_bedrock_profiles = ["us.amazon.nova-micro-v1:0"] # answer generation
  web_bedrock_models   = ["amazon.titan-embed-text-v2:0", "cohere.rerank-v3-5:0"]
}

data "aws_iam_policy_document" "docqa_ingest" {
  statement {
    sid       = "ReadRawDocuments"
    actions   = ["s3:GetObject"]
    resources = ["${module.docqa_bucket.bucket_arn}/raw/*"]
  }

  statement {
    sid       = "ReadWriteDerivedData"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${module.docqa_bucket.bucket_arn}/parsed/*", "${module.docqa_bucket.bucket_arn}/lancedb/*"]
  }

  statement {
    sid       = "ListOwnPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [module.docqa_bucket.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["raw/*", "parsed/*", "lancedb/*", "lancedb"]
    }
  }

  statement {
    sid       = "UseBucketKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }

  # Inference profiles route to any US region, so the foundation-model ARN needs a wildcard
  # region alongside the profile ARN.
  statement {
    sid     = "InvokeModels"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [for p in local.bedrock_profiles : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${p}"],
      [for p in local.bedrock_profiles : "arn:aws:bedrock:*::foundation-model/${trimprefix(p, "us.")}"],
      ["arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"],
    )
  }
}

resource "aws_lambda_permission" "docqa_ingest_from_s3" {
  provider       = aws.docqa
  statement_id   = "AllowDocsBucketNotifications"
  action         = "lambda:InvokeFunction"
  function_name  = module.docqa_ingest.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = module.docqa_bucket.bucket_arn
  source_account = local.account_id
}

resource "aws_s3_bucket_notification" "docqa_raw" {
  provider = aws.docqa
  bucket   = module.docqa_bucket.bucket_name

  lambda_function {
    lambda_function_arn = module.docqa_ingest.function_arn
    events              = ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
    filter_prefix       = "raw/"
  }

  depends_on = [aws_lambda_permission.docqa_ingest_from_s3]
}

# S3 invokes asynchronously: retry once (e.g. after throttling), drop events older than 6 h.
resource "aws_lambda_function_event_invoke_config" "docqa_ingest" {
  provider                     = aws.docqa
  function_name                = module.docqa_ingest.function_name
  maximum_retry_attempts       = 1
  maximum_event_age_in_seconds = 21600
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
