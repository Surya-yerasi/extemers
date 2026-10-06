data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

# Least privilege: write only to this function's own log group, plus X-Ray.
data "aws_iam_policy_document" "base" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  statement {
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "base" {
  name   = "base"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.base.json
}

resource "aws_iam_role_policy" "additional" {
  count  = var.additional_policy_json == null ? 0 : 1
  name   = "additional"
  role   = aws_iam_role.this.id
  policy = var.additional_policy_json
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.this.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = [var.architecture]
  memory_size   = var.memory_size
  timeout       = var.timeout

  dynamic "image_config" {
    for_each = var.image_command == null ? [] : [1]
    content {
      command = var.image_command
    }
  }

  environment {
    variables = var.environment_variables
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [aws_iam_role_policy.base]
}

resource "aws_lambda_function_url" "this" {
  count              = var.function_url_enabled ? 1 : 0
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"
}

# A public Function URL needs both permissions: InvokeFunctionUrl, and InvokeFunction
# restricted to calls arriving through the Function URL.
resource "aws_lambda_permission" "url_invoke_function_url" {
  count                  = var.function_url_enabled ? 1 : 0
  statement_id           = "FunctionUrlPublicInvokeUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke_function" {
  count                    = var.function_url_enabled ? 1 : 0
  statement_id             = "FunctionUrlPublicInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.this.function_name
  principal                = "*"
  invoked_via_function_url = true
}
