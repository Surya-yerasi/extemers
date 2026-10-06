# Customer-managed key for docqa data: the documents bucket, SSM secrets and log groups.
# Lives in bootstrap (human-applied) so CI can use it but never schedule its deletion.
data "aws_iam_policy_document" "docqa_kms" {
  # Account root administers the key; IAM policies then grant use to specific roles.
  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  # CloudWatch Logs may encrypt only docqa Lambda log groups with this key.
  statement {
    sid = "CloudWatchLogsForDocqaLogGroups"
    actions = [
      "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/docqa-*"]
    }
  }
}

resource "aws_kms_key" "docqa" {
  description             = "docqa: documents bucket, SSM secrets, log groups"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.docqa_kms.json

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "docqa" {
  name          = "alias/docqa"
  target_key_id = aws_kms_key.docqa.key_id
}
