locals {
  forward_profile_arn = "arn:aws:bedrock:eu-central-1:${var.aws_account_id}:inference-profile/eu.amazon.nova-micro-v1:0"
  forward_model_arns = [
    for region in ["eu-central-1", "eu-north-1", "eu-west-1", "eu-west-3"] :
    "arn:aws:bedrock:${region}::foundation-model/amazon.nova-micro-v1:0"
  ]
  forward_alarm_arns = [
    for name in ["forward-worker-errors", "forward-delivery-failures"] :
    "arn:aws:cloudwatch:${var.aws_region}:${var.aws_account_id}:alarm:${var.name_prefix}-${name}"
  ]
}

resource "aws_iam_role_policy" "forward_runtime" {
  count = var.enable_forward_runtime ? 1 : 0
  name  = "ForwardPaperEvidenceAndAlerts"
  role  = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.state.arn}/paper/forward/state.json",
          "${aws_s3_bucket.state.arn}/paper/forward/inputs/runtime.json",
          "${aws_s3_bucket.state.arn}/paper/forward/inputs/config.json",
          "${aws_s3_bucket.state.arn}/paper/forward/inputs/plan.json",
          "${aws_s3_bucket.state.arn}/paper/forward/ticks/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.state.arn}/paper/forward/state.json",
          "${aws_s3_bucket.state.arn}/paper/forward/ticks/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.forward_alerts[0].arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "forward_model" {
  count = var.enable_forward_model_calls ? 1 : 0
  name  = "ReviewedNovaMicroProfileOnly"
  role  = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = [local.forward_profile_arn]
      },
      {
        Effect    = "Allow"
        Action    = ["bedrock:InvokeModel"]
        Resource  = local.forward_model_arns
        Condition = { StringEquals = { "bedrock:InferenceProfileArn" = local.forward_profile_arn } }
      }
    ]
  })
}

resource "aws_sns_topic" "forward_alerts" {
  count = var.enable_forward_runtime ? 1 : 0
  name  = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_policy" "forward_alerts" {
  count = var.enable_forward_runtime ? 1 : 0
  arn   = aws_sns_topic.forward_alerts[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "OwnAccountTopicManagement"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
        Action    = "SNS:*"
        Resource  = aws_sns_topic.forward_alerts[0].arn
      },
      {
        Sid       = "OwnPaperAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.forward_alerts[0].arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = var.aws_account_id }
          ArnEquals    = { "aws:SourceArn" = local.forward_alarm_arns }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "forward_alerts" {
  count     = var.enable_forward_runtime ? 1 : 0
  topic_arn = aws_sns_topic.forward_alerts[0].arn
  protocol  = "email"
  endpoint  = var.forward_alert_email
}

resource "aws_cloudwatch_metric_alarm" "forward_worker_errors" {
  count               = var.enable_forward_runtime ? 1 : 0
  alarm_name          = "${var.name_prefix}-forward-worker-errors"
  alarm_description   = "ETF paper worker failure; review retained private evidence before recovery"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.worker.function_name }
  alarm_actions       = [aws_sns_topic.forward_alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "forward_delivery_failures" {
  count               = var.enable_forward_runtime ? 1 : 0
  alarm_name          = "${var.name_prefix}-forward-delivery-failures"
  alarm_description   = "ETF paper failure queue has retained events for operator review"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.failures.name }
  alarm_actions       = [aws_sns_topic.forward_alerts[0].arn]
}

resource "aws_scheduler_schedule" "forward" {
  count                        = var.enable_forward_runtime ? 1 : 0
  name                         = "${var.name_prefix}-forward"
  group_name                   = aws_scheduler_schedule_group.paper.name
  description                  = "One bounded daily issuer-data paper tick; no live trading"
  state                        = var.enable_forward_schedule ? "ENABLED" : "DISABLED"
  schedule_expression          = "cron(0 8 * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = aws_iam_role.schedule.arn
    input    = jsonencode({ action = "forward_tick", scheduled_at = "<aws.scheduler.scheduled-time>" })
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
    dead_letter_config { arn = aws_sqs_queue.failures.arn }
  }

  depends_on = [aws_iam_role_policy.schedule, aws_lambda_function_event_invoke_config.worker]
}
