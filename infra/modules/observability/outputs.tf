output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.this.dashboard_name}"
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "alarm_names" {
  value = concat([for a in aws_cloudwatch_metric_alarm.count : a.alarm_name], [aws_cloudwatch_metric_alarm.ask_latency_p95.alarm_name])
}
