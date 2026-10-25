locals {
  worker_name = "${var.name_prefix}-worker"
  package     = "${path.module}/../var/lambda.zip"
}

resource "aws_s3_bucket" "state" {
  bucket        = "${var.name_prefix}-${var.aws_account_id}-${var.aws_region}"
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket     = aws_s3_bucket.state.id
  depends_on = [aws_s3_bucket_versioning.state]

  rule {
    id     = "OldStateVersions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "RequireConditionalStateWrites"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.state.arn}/paper/state.json"
        Condition = { Null = { "s3:if-match" = "true", "s3:if-none-match" = "true" } }
      }
    ]
  })
}

resource "aws_sqs_queue" "failures" {
  name                      = "${var.name_prefix}-failures"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.worker_name}"
  retention_in_days = 7

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role" "worker" {
  name = "${var.name_prefix}-worker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "PaperStateOnly"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.state.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.state.arn}/paper/state.json", "${aws_s3_bucket.state.arn}/paper/inputs/current.json"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.state.arn}/paper/state.json"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.worker.arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.failures.arn]
      }
    ]
  })
}

resource "aws_lambda_function" "worker" {
  function_name                  = local.worker_name
  filename                       = local.package
  source_code_hash               = filebase64sha256(local.package)
  handler                        = "etf_autopilot.aws_handler.handler"
  runtime                        = "python3.13"
  architectures                  = ["x86_64"]
  memory_size                    = 256
  timeout                        = 30
  reserved_concurrent_executions = var.worker_reserved_concurrency
  role                           = aws_iam_role.worker.arn

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.worker.name
  }

  environment {
    variables = {
      EXECUTION_MODE = "paper"
      STATE_BUCKET   = aws_s3_bucket.state.id
    }
  }

  # Configure storage protections and permissions before the worker becomes invocable.
  depends_on = [
    aws_iam_role_policy.worker,
    aws_s3_bucket_policy.state,
    aws_s3_bucket_public_access_block.state,
    aws_s3_bucket_server_side_encryption_configuration.state,
    aws_s3_bucket_versioning.state,
  ]
}

resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name                = aws_lambda_function.worker.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300

  destination_config {
    on_failure {
      destination = aws_sqs_queue.failures.arn
    }
  }
}

resource "aws_scheduler_schedule_group" "paper" {
  name = var.name_prefix
}

resource "aws_iam_role" "schedule" {
  name = "${var.name_prefix}-schedule"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.aws_account_id
          "aws:SourceArn"     = aws_scheduler_schedule_group.paper.arn
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "schedule" {
  name = "InvokePaperWorker"
  role = aws_iam_role.schedule.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.worker.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.failures.arn]
      }
    ]
  })
}

resource "aws_scheduler_schedule" "health" {
  name                         = "${var.name_prefix}-health"
  group_name                   = aws_scheduler_schedule_group.paper.name
  description                  = "Daily paper health only; no orders, prices or model calls"
  state                        = var.enable_health_schedule ? "ENABLED" : "DISABLED"
  schedule_expression          = "cron(0 18 * * ? *)"
  schedule_expression_timezone = "Europe/Berlin"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = aws_iam_role.schedule.arn
    input    = jsonencode({ action = "health" })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }

    dead_letter_config {
      arn = aws_sqs_queue.failures.arn
    }
  }

  depends_on = [aws_iam_role_policy.schedule, aws_lambda_function_event_invoke_config.worker]
}
