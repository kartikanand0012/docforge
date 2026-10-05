resource "aws_sns_topic" "alerts" {
  name              = "docforge-demo-alerts"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# The ops check reports 0 ok, 1 warning, 2 critical every five minutes. Critical twice, or no
# report at all for 15 minutes (the host or its timer is down), emails the owner.
resource "aws_cloudwatch_metric_alarm" "ops" {
  alarm_name          = "docforge-demo-ops-critical"
  namespace           = "DocForge"
  metric_name         = "OpsStatus"
  dimensions          = { Stack = "demo" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 2
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "host" {
  alarm_name          = "docforge-demo-host-status"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.demo.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  name         = "docforge-demo-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [["ACTUAL", 80], ["ACTUAL", 100], ["FORECASTED", 100]]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value[1]
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value[0]
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
