# All runs use generated provider data at plan time, never AWS credentials/APIs.
mock_provider "aws" {
  override_during = plan

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::etf-autopilot-paper-123456789012-eu-central-1"
      id  = "etf-autopilot-paper-123456789012-eu-central-1"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/lambda/etf-autopilot-paper-worker" }
  }

  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:eu-central-1:123456789012:etf-autopilot-paper-failures"
      url = "https://sqs.eu-central-1.amazonaws.com/123456789012/etf-autopilot-paper-failures"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/fixture-only" }
  }

  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:eu-central-1:123456789012:function:etf-autopilot-paper-worker" }
  }

  mock_resource "aws_scheduler_schedule_group" {
    defaults = { arn = "arn:aws:scheduler:eu-central-1:123456789012:schedule-group/etf-autopilot-paper" }
  }

  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:eu-central-1:123456789012:etf-autopilot-paper-alerts" }
  }
}

variables {
  aws_account_id = "123456789012"
  aws_region     = "eu-central-1"
}

run "safe_defaults" {
  command = plan

  assert {
    condition = (
      aws_lambda_function.worker.environment[0].variables.EXECUTION_MODE == "paper" &&
      aws_lambda_function.worker.reserved_concurrent_executions == 1 &&
      aws_lambda_function.worker.memory_size == 256 &&
      aws_lambda_function.worker.timeout == 30 &&
      aws_lambda_function.worker.runtime == "python3.14"
    )
    error_message = "The worker must remain paper-only, serial and bounded in memory/runtime."
  }

  assert {
    condition = (
      aws_scheduler_schedule.health.state == "DISABLED" &&
      jsondecode(aws_scheduler_schedule.health.target[0].input) == { action = "health" } &&
      aws_scheduler_schedule.health.schedule_expression_timezone == "Europe/Berlin"
    )
    error_message = "Scheduling must be opt-in and must only check health."
  }

  assert {
    condition = (
      aws_lambda_function_event_invoke_config.worker.maximum_retry_attempts == 0 &&
      aws_scheduler_schedule.health.target[0].retry_policy[0].maximum_retry_attempts == 0 &&
      aws_scheduler_schedule.health.target[0].dead_letter_config[0].arn == aws_sqs_queue.failures.arn &&
      aws_lambda_function_event_invoke_config.worker.destination_config[0].on_failure[0].destination == aws_sqs_queue.failures.arn
    )
    error_message = "Automatic delivery retries must remain off and failures must be retained."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets &&
      aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" &&
      !aws_s3_bucket.state.force_destroy
    )
    error_message = "Application state must stay private, encrypted, versioned and protected from forced emptying."
  }

  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Effect == "Deny" &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[1].Condition.Null == { "s3:if-match" = "true", "s3:if-none-match" = "true" } &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[1].Resource == "${aws_s3_bucket.state.arn}/paper/state.json"
    )
    error_message = "Bucket policy must reject insecure access and unconditional ledger overwrites."
  }

  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.worker.policy).Statement : statement.Action])) == toset([
        "s3:ListBucket", "s3:GetObject", "s3:PutObject", "logs:CreateLogStream", "logs:PutLogEvents", "sqs:SendMessage"
      ]) &&
      toset(jsondecode(aws_iam_role_policy.worker.policy).Statement[1].Resource) == toset([
        "${aws_s3_bucket.state.arn}/paper/state.json", "${aws_s3_bucket.state.arn}/paper/inputs/current.json"
      ]) &&
      toset(jsondecode(aws_iam_role_policy.worker.policy).Statement[2].Resource) == toset(["${aws_s3_bucket.state.arn}/paper/state.json"])
    )
    error_message = "The worker may access only its paper state/input, logs and failure queue; no model, secret or broker authority."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.schedule.assume_role_policy).Statement[0].Principal.Service == "scheduler.amazonaws.com" &&
      jsondecode(aws_iam_role.schedule.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == var.aws_account_id &&
      jsondecode(aws_iam_role.schedule.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == aws_scheduler_schedule_group.paper.arn &&
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.schedule.policy).Statement : statement.Action])) == toset(["lambda:InvokeFunction", "sqs:SendMessage"]) &&
      toset(jsondecode(aws_iam_role_policy.schedule.policy).Statement[0].Resource) == toset([aws_lambda_function.worker.arn])
    )
    error_message = "Scheduler trust must be restricted to this account/group and invocation to this worker."
  }

  assert {
    condition = (
      aws_cloudwatch_log_group.worker.retention_in_days == 7 &&
      aws_sqs_queue.failures.sqs_managed_sse_enabled &&
      aws_sqs_queue.failures.message_retention_seconds == 1209600
    )
    error_message = "Logs need bounded retention and failure messages must be encrypted and retained for review."
  }
}

run "health_requires_explicit_opt_in" {
  command = plan

  variables {
    enable_health_schedule = true
  }

  assert {
    condition     = aws_scheduler_schedule.health.state == "ENABLED" && jsondecode(aws_scheduler_schedule.health.target[0].input).action == "health"
    error_message = "Enabling the schedule must never turn it into an investment decision trigger."
  }
}

run "stop_without_deleting_state" {
  command = plan

  variables {
    worker_reserved_concurrency = 0
  }

  assert {
    condition     = aws_lambda_function.worker.reserved_concurrent_executions == 0 && aws_scheduler_schedule.health.state == "DISABLED"
    error_message = "The worker and scheduled activity must be stoppable without deleting the ledger."
  }
}

run "reject_parallel_execution" {
  command = plan

  variables {
    worker_reserved_concurrency = 2
  }

  expect_failures = [var.worker_reserved_concurrency]
}

run "forward_is_disabled_by_default" {
  command = plan

  assert {
    condition = (
      length(aws_scheduler_schedule.forward) == 0 &&
      length(aws_iam_role_policy.forward_model) == 0 &&
      length(aws_iam_role_policy.forward_runtime) == 0 &&
      length(aws_sns_topic.forward_alerts) == 0 &&
      !contains(keys(aws_lambda_function.worker.environment[0].variables), "FORWARD_ENABLED")
    )
    error_message = "Forward capture, inference and alerts must require a separate opt-in."
  }
}

run "forward_preparation_keeps_worker_and_schedules_stopped" {
  command = plan

  variables {
    enable_forward_runtime      = true
    forward_runtime_hash        = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    forward_alert_email         = "fixture@example.invalid"
    worker_reserved_concurrency = 0
  }

  assert {
    condition = (
      aws_lambda_function.worker.memory_size == 512 &&
      aws_lambda_function.worker.timeout == 180 &&
      aws_lambda_function.worker.reserved_concurrent_executions == 0 &&
      aws_scheduler_schedule.forward[0].state == "DISABLED" &&
      aws_scheduler_schedule.health.state == "DISABLED" &&
      length(aws_iam_role_policy.forward_model) == 0 &&
      aws_lambda_function.worker.environment[0].variables.FORWARD_ALLOW_MODEL_CALLS == "false"
    )
    error_message = "Preparing the runtime cannot activate capture or inference."
  }

  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[2].Condition.Null == { "s3:if-match" = "true", "s3:if-none-match" = "true" } &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[2].Resource == "${aws_s3_bucket.state.arn}/paper/forward/state.json" &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[3].Condition.Null == { "s3:if-none-match" = "true" } &&
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[3].Resource == "${aws_s3_bucket.state.arn}/paper/forward/ticks/*"
    )
    error_message = "Forward state requires CAS; daily evidence requires immutable conditional creation."
  }

  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.forward_runtime[0].policy).Statement : statement.Action])) == toset(["s3:GetObject", "s3:PutObject", "sns:Publish"]) &&
      toset(jsondecode(aws_iam_role_policy.forward_runtime[0].policy).Statement[1].Resource) == toset([
        "${aws_s3_bucket.state.arn}/paper/forward/state.json", "${aws_s3_bucket.state.arn}/paper/forward/ticks/*"
      ]) &&
      jsondecode(aws_iam_role_policy.forward_runtime[0].policy).Statement[2].Resource == [aws_sns_topic.forward_alerts[0].arn]
    )
    error_message = "Forward writes must exclude runtime inputs, secrets and unrelated applications."
  }

  assert {
    condition = (
      aws_sns_topic_subscription.forward_alerts[0].protocol == "email" &&
      jsondecode(aws_sns_topic_policy.forward_alerts[0].policy).Statement[1].Condition.StringEquals["aws:SourceAccount"] == var.aws_account_id &&
      toset(jsondecode(aws_sns_topic_policy.forward_alerts[0].policy).Statement[1].Condition.ArnEquals["aws:SourceArn"]) == toset([
        "arn:aws:cloudwatch:${var.aws_region}:${var.aws_account_id}:alarm:${aws_cloudwatch_metric_alarm.forward_worker_errors[0].alarm_name}",
        "arn:aws:cloudwatch:${var.aws_region}:${var.aws_account_id}:alarm:${aws_cloudwatch_metric_alarm.forward_delivery_failures[0].alarm_name}"
      ]) &&
      aws_cloudwatch_metric_alarm.forward_worker_errors[0].dimensions == tomap({ FunctionName = aws_lambda_function.worker.function_name }) &&
      aws_cloudwatch_metric_alarm.forward_delivery_failures[0].dimensions == tomap({ QueueName = aws_sqs_queue.failures.name })
    )
    error_message = "Notifications must bind only this account's exact worker and failure-queue alarms."
  }
}

run "forward_activation_is_bounded" {
  command = plan

  variables {
    enable_forward_runtime     = true
    enable_forward_model_calls = true
    enable_forward_schedule    = true
    forward_alert_confirmed    = true
    forward_runtime_hash       = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    forward_alert_email        = "fixture@example.invalid"
  }

  assert {
    condition = (
      aws_scheduler_schedule.forward[0].state == "ENABLED" &&
      aws_scheduler_schedule.forward[0].schedule_expression == "cron(0 8 * * ? *)" &&
      aws_scheduler_schedule.forward[0].schedule_expression_timezone == "UTC" &&
      jsondecode(aws_scheduler_schedule.forward[0].target[0].input) == {
        action = "forward_tick", scheduled_at = "<aws.scheduler.scheduled-time>"
      } &&
      aws_scheduler_schedule.forward[0].target[0].retry_policy[0].maximum_retry_attempts == 0 &&
      aws_scheduler_schedule.forward[0].target[0].retry_policy[0].maximum_event_age_in_seconds == 300 &&
      aws_scheduler_schedule.health.state == "DISABLED"
    )
    error_message = "Only one daily UTC paper trigger is authorized, with no automatic delivery retry."
  }

  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.forward_model[0].policy).Statement : statement.Action])) == toset(["bedrock:InvokeModel"]) &&
      jsondecode(aws_iam_role_policy.forward_model[0].policy).Statement[0].Resource == ["arn:aws:bedrock:eu-central-1:123456789012:inference-profile/eu.amazon.nova-micro-v1:0"] &&
      length(jsondecode(aws_iam_role_policy.forward_model[0].policy).Statement[1].Resource) == 4 &&
      jsondecode(aws_iam_role_policy.forward_model[0].policy).Statement[1].Condition.StringEquals["bedrock:InferenceProfileArn"] == "arn:aws:bedrock:eu-central-1:123456789012:inference-profile/eu.amazon.nova-micro-v1:0"
    )
    error_message = "Inference IAM must grant only the exact EU Nova Micro profile and its four reviewed routes."
  }
}

run "reject_schedule_before_alert_confirmation" {
  command = plan
  variables {
    enable_forward_runtime  = true
    enable_forward_schedule = true
    forward_runtime_hash    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    forward_alert_email     = "fixture@example.invalid"
  }
  expect_failures = [var.enable_forward_schedule]
}

run "reject_model_without_runtime" {
  command = plan
  variables { enable_forward_model_calls = true }
  expect_failures = [var.enable_forward_model_calls]
}
