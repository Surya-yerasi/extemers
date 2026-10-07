# One dashboard, five alarms and an email topic for a web + ingest Lambda pair.
# All within the CloudWatch free tier (3 dashboards, 10 alarms) and SNS free tier.

data "aws_caller_identity" "current" {}

locals {
  app_dimensions = { service = "docqa-web", environment = var.environment }
  web_log_group  = "/aws/lambda/${var.web_function_name}"
  ingest_logs    = "/aws/lambda/${var.ingest_function_name}"
}

# Alarm notifications carry metric names and states only (no document data), so the topic
# uses no customer-managed KMS key: CloudWatch could not publish to one without a key-policy
# change in bootstrap.
resource "aws_sns_topic" "alarms" {
  name = "${var.name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  alarms = {
    web-5xx = {
      description = "The web app returned server errors (includes model quota and provider failures)."
      namespace   = "AWS/Lambda"
      metric      = "Url5xxCount"
      dimensions  = { FunctionName = var.web_function_name }
      statistic   = "Sum"
      period      = 300
      threshold   = 1
    }
    web-throttles = {
      description = "Lambda throttled the web function (account concurrency is 10)."
      namespace   = "AWS/Lambda"
      metric      = "Throttles"
      dimensions  = { FunctionName = var.web_function_name }
      statistic   = "Sum"
      period      = 300
      threshold   = 1
    }
    ingest-errors = {
      description = "Ingestion failed (see ingest_failed in the ingest logs: doc_id and error_code)."
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      dimensions  = { FunctionName = var.ingest_function_name }
      statistic   = "Sum"
      period      = 300
      threshold   = 1
    }
    model-errors = {
      description = "A model provider call failed (throttling, access, timeouts). Open the trace by its ID."
      namespace   = var.metrics_namespace
      metric      = "ModelErrors"
      dimensions  = local.app_dimensions
      statistic   = "Sum"
      period      = 300
      threshold   = 1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "count" {
  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = each.value.statistic
  period              = each.value.period
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching" # no traffic is not a problem
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "ask_latency_p95" {
  alarm_name          = "${var.name}-ask-latency-p95"
  alarm_description   = "P95 time per question is above ${var.ask_latency_p95_threshold_ms} ms over 15 minutes."
  namespace           = var.metrics_namespace
  metric_name         = "AskLatency"
  dimensions          = local.app_dimensions
  extended_statistic  = "p95"
  period              = 900
  evaluation_periods  = 1
  threshold           = var.ask_latency_p95_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

locals {
  app_metric = [var.metrics_namespace]
  app_dims   = ["service", local.app_dimensions.service, "environment", local.app_dimensions.environment]

  widgets = [
    {
      type = "text", x = 0, y = 0, width = 24, height = 2
      properties = {
        markdown = "## ${var.name}\nQuestions, latency and cost come from the app's EMF metrics (namespace `${var.metrics_namespace}`). Every question has a trace ID (`c_<hex>.<n>`): paste it into the app's **Traces** tab, or filter the log tables below with it."
      }
    },
    {
      type = "metric", x = 0, y = 2, width = 8, height = 6
      properties = {
        title = "Web: requests and errors", region = var.region, stat = "Sum", period = 300, view = "timeSeries"
        metrics = [
          ["AWS/Lambda", "UrlRequestCount", "FunctionName", var.web_function_name, { label = "requests" }],
          [".", "Url4xxCount", ".", ".", { label = "4xx" }],
          [".", "Url5xxCount", ".", ".", { label = "5xx", color = "#d62728" }],
          [".", "Throttles", ".", ".", { label = "throttles" }],
        ]
      }
    },
    {
      type = "metric", x = 8, y = 2, width = 8, height = 6
      properties = {
        title = "Time per question (ms)", region = var.region, period = 300, view = "timeSeries"
        metrics = [
          concat(local.app_metric, ["AskLatency"], local.app_dims, [{ stat = "p50", label = "total p50" }]),
          concat(local.app_metric, ["AskLatency"], local.app_dims, [{ stat = "p95", label = "total p95" }]),
          concat(local.app_metric, ["RetrievalLatency"], local.app_dims, [{ stat = "p95", label = "retrieval p95" }]),
          concat(local.app_metric, ["GenerationLatency"], local.app_dims, [{ stat = "p95", label = "generation p95" }]),
        ]
        annotations = { horizontal = [{ value = var.ask_latency_p95_threshold_ms, label = "alarm" }] }
      }
    },
    {
      type = "metric", x = 16, y = 2, width = 8, height = 6
      properties = {
        title = "Cost, not found, model errors", region = var.region, stat = "Sum", period = 3600, view = "timeSeries"
        metrics = [
          concat(local.app_metric, ["CostUSD"], local.app_dims, [{ label = "model cost (USD)" }]),
          concat(local.app_metric, ["NotFound"], local.app_dims, [{ label = "not found", yAxis = "right" }]),
          concat(local.app_metric, ["ModelErrors"], local.app_dims, [{ label = "model errors", yAxis = "right", color = "#d62728" }]),
        ]
      }
    },
    {
      type = "metric", x = 0, y = 8, width = 8, height = 6
      properties = {
        title = "Lambda duration (ms)", region = var.region, period = 300, view = "timeSeries"
        metrics = [
          ["AWS/Lambda", "Duration", "FunctionName", var.web_function_name, { stat = "p95", label = "web p95" }],
          ["...", var.ingest_function_name, { stat = "Maximum", label = "ingest max" }],
        ]
      }
    },
    {
      type = "metric", x = 8, y = 8, width = 8, height = 6
      properties = {
        title = "Ingestion", region = var.region, stat = "Sum", period = 300, view = "timeSeries"
        metrics = [
          ["AWS/Lambda", "Invocations", "FunctionName", var.ingest_function_name, { label = "events" }],
          [".", "Errors", ".", ".", { label = "errors", color = "#d62728" }],
        ]
      }
    },
    {
      type = "alarm", x = 16, y = 8, width = 8, height = 6
      properties = {
        title = "Alarms"
        # Built from names, not resource attributes, so the whole body is known at plan time
        # and reviewable in the PR's plan.
        alarms = [
          for n in concat(keys(local.alarms), ["ask-latency-p95"]) :
          "arn:aws:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${var.name}-${n}"
        ]
      }
    },
    {
      type = "log", x = 0, y = 14, width = 24, height = 7
      properties = {
        title = "Recent questions (no question text is logged)", region = var.region, view = "table"
        query = "SOURCE '${local.web_log_group}' | fields @timestamp, trace_id, strategy, timings_ms.total as total_ms, timings_ms.generate as generate_ms, not_found, citations, cost_usd, rewritten | filter message = \"turn_completed\" | sort @timestamp desc | limit 25"
      }
    },
    {
      type = "log", x = 0, y = 21, width = 12, height = 6
      properties = {
        title = "Model and provider errors", region = var.region, view = "table"
        query = "SOURCE '${local.web_log_group}' | fields @timestamp, trace_id, error_code, message | filter message in [\"ask_upstream_error\", \"ask_model_http_error\"] | sort @timestamp desc | limit 25"
      }
    },
    {
      type = "log", x = 12, y = 21, width = 12, height = 6
      properties = {
        title = "Ingestion results and failures", region = var.region, view = "table"
        query = "SOURCE '${local.ingest_logs}' | fields @timestamp, message, doc_id, chunks, vision_pages, error_code | filter message in [\"document_indexed\", \"document_deleted\", \"ingest_failed\"] | sort @timestamp desc | limit 25"
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({ widgets = local.widgets })
}
