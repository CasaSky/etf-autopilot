output "state_bucket_name" {
  description = "Application ledger/input bucket; distinct from Terraform's own state."
  value       = aws_s3_bucket.state.id
}

output "worker_name" {
  value = aws_lambda_function.worker.function_name
}

output "health_schedule_name" {
  value = aws_scheduler_schedule.health.name
}

output "health_schedule_group" {
  value = aws_scheduler_schedule_group.paper.name
}

output "failure_queue_url" {
  value = aws_sqs_queue.failures.url
}

output "forward_schedule_name" {
  value = try(aws_scheduler_schedule.forward[0].name, null)
}

output "forward_alert_topic_arn" {
  value = try(aws_sns_topic.forward_alerts[0].arn, null)
}
