data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "extemers-tfstate-${local.account_id}"
  oidc_host    = "token.actions.githubusercontent.com"
  oidc_arn = (var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
  : "arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_host}")
}

# ---------------------------------------------------------------- state bucket
resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "state_tls_only" {
  statement {
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_tls_only.json
}

# ---------------------------------------------------------------- GitHub OIDC
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "state_access" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
}

# Plan role: pull requests only. Read-only on AWS + state/lock access.
data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["${var.github_subject_prefix}:pull_request"]
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = "extemers-github-plan"
  assume_role_policy   = data.aws_iam_policy_document.trust_plan.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "terraform-state"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.state_access.json
}

# Deploy role: only jobs running in the protected GitHub environment.
data "aws_iam_policy_document" "trust_deploy" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["${var.github_subject_prefix}:environment:${var.deploy_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "extemers-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.trust_deploy.json
  max_session_duration = 3600
}

locals {
  lambda_arns = [for p in var.managed_name_prefixes :
  "arn:aws:lambda:${var.region}:${local.account_id}:function:${p}*"]
  role_arns = [for p in var.managed_name_prefixes :
  "arn:aws:iam::${local.account_id}:role/${p}*"]
  log_group_arns = [for p in var.managed_name_prefixes :
  "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${p}*"]
  ecr_repo_arns = [for p in var.managed_name_prefixes :
  "arn:aws:ecr:${var.region}:${local.account_id}:repository/${p}*"]
  bucket_arns        = [for p in var.managed_name_prefixes : "arn:aws:s3:::${p}*"]
  bucket_object_arns = [for p in var.managed_name_prefixes : "arn:aws:s3:::${p}*/*"]
  ssm_parameter_arns = [for p in var.managed_name_prefixes :
  "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${trimsuffix(p, "-")}/*"]
  budget_arns = [for p in var.managed_name_prefixes :
  "arn:aws:budgets::${local.account_id}:budget/${p}*"]
  dashboard_arns = [for p in var.managed_name_prefixes :
  "arn:aws:cloudwatch::${local.account_id}:dashboard/${p}*"]
  alarm_arns = [for p in var.managed_name_prefixes :
  "arn:aws:cloudwatch:${var.region}:${local.account_id}:alarm:${p}*"]
  # Matches topics and their subscriptions (<topic arn>:<subscription id>).
  sns_arns = [for p in var.managed_name_prefixes :
  "arn:aws:sns:${var.region}:${local.account_id}:${p}*"]
}

data "aws_iam_policy_document" "deploy" {
  source_policy_documents = [data.aws_iam_policy_document.state_access.json]

  statement {
    sid       = "Lambda"
    actions   = ["lambda:*"]
    resources = local.lambda_arns
  }

  statement {
    sid = "LambdaExecutionRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
      "iam:TagRole", "iam:UntagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
      "iam:UpdateAssumeRolePolicy", "iam:ListInstanceProfilesForRole",
    ]
    resources = local.role_arns
  }

  statement {
    sid       = "PassRoleToLambdaOnly"
    actions   = ["iam:PassRole"]
    resources = local.role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid       = "LogGroups"
    actions   = ["logs:*"]
    resources = concat(local.log_group_arns, [for a in local.log_group_arns : "${a}:*"])
  }

  statement {
    sid       = "LogsAccountLevel"
    actions   = ["logs:DescribeLogGroups", "logs:ListTagsForResource"]
    resources = ["*"]
  }

  statement {
    sid       = "EcrRepositories"
    actions   = ["ecr:*"]
    resources = local.ecr_repo_arns
  }

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # account-level action; cannot be scoped
  }

  # Bucket configuration only. The deny below keeps CI away from the objects (your documents).
  statement {
    sid = "BucketConfiguration"
    actions = [
      "s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucket", "s3:ListBucketVersions",
      "s3:GetBucket*", "s3:PutBucket*", "s3:DeleteBucketPolicy",
      "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
      "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration",
      "s3:GetAccelerateConfiguration", "s3:GetReplicationConfiguration",
      "s3:GetAnalyticsConfiguration", "s3:GetInventoryConfiguration", "s3:GetMetricsConfiguration",
      "s3:GetIntelligentTieringConfiguration", "s3:GetBucketObjectLockConfiguration",
    ]
    resources = local.bucket_arns
  }

  statement {
    sid       = "DenyDocumentObjects"
    effect    = "Deny"
    actions   = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject"]
    resources = local.bucket_object_arns
  }

  statement {
    sid       = "UseServiceKms"
    actions   = ["kms:DescribeKey"]
    resources = [aws_kms_key.docqa.arn]
  }

  # Cognito user pools have generated IDs, so they cannot be scoped by name.
  statement {
    sid       = "Cognito"
    actions   = ["cognito-idp:*"]
    resources = ["arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/*"]
  }

  statement {
    sid       = "CognitoAccountLevel"
    actions   = ["cognito-idp:CreateUserPool", "cognito-idp:ListUserPools", "cognito-idp:DescribeUserPoolDomain"]
    resources = ["*"]
  }

  statement {
    sid       = "SsmParameters"
    actions   = ["ssm:*"]
    resources = local.ssm_parameter_arns
  }

  statement {
    sid       = "SsmDescribe"
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }

  statement {
    sid       = "Budgets"
    actions   = ["budgets:*"]
    resources = local.budget_arns
  }

  statement {
    sid       = "Dashboards"
    actions   = ["cloudwatch:PutDashboard", "cloudwatch:GetDashboard", "cloudwatch:DeleteDashboards"]
    resources = local.dashboard_arns
  }

  statement {
    sid = "Alarms"
    actions = [
      "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms",
      "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource",
    ]
    resources = local.alarm_arns
  }

  statement {
    sid = "AlarmTopics"
    actions = [
      "sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes",
      "sns:Subscribe", "sns:ListSubscriptionsByTopic",
      "sns:TagResource", "sns:UntagResource", "sns:ListTagsForResource",
    ]
    resources = local.sns_arns
  }

  # SNS does not support resource-level permissions for subscription actions: they are
  # authorised against "*" only (found with simulate-principal-policy before the first apply).
  # No data access: reading or removing an email subscription's settings.
  statement {
    sid       = "AlarmSubscriptions"
    actions   = ["sns:GetSubscriptionAttributes", "sns:SetSubscriptionAttributes", "sns:Unsubscribe"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
