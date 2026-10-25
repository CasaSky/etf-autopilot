variable "aws_account_id" {
  description = "Explicit target account; the provider refuses credentials for another account."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Choose the exact 12-digit AWS account ID before planning deployment."
  }
}

variable "aws_region" {
  description = "Explicit target region; no deployment region is selected implicitly."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region)) && length(var.aws_region) <= 16
    error_message = "Use a valid AWS region identifier of at most 16 characters."
  }
}

variable "aws_profile" {
  description = "Optional local AWS CLI profile; otherwise use the normal runtime credential chain."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Isolated names for this application; never point this module at a sibling application's resources."
  type        = string
  default     = "etf-autopilot-paper"

  validation {
    condition     = can(regex("^etf-autopilot-[a-z0-9-]+[a-z0-9]$", var.name_prefix)) && length(var.name_prefix) <= 24
    error_message = "Use an etf-autopilot- prefix, lowercase letters/digits/hyphens and at most 24 characters."
  }
}

variable "enable_health_schedule" {
  description = "Opt in to the daily state-health check only after reviewing deployment and costs."
  type        = bool
  default     = false
}

variable "worker_reserved_concurrency" {
  description = "One serial paper worker, or zero to stop invocation without deleting its state."
  type        = number
  default     = 1

  validation {
    condition     = contains([0, 1], var.worker_reserved_concurrency)
    error_message = "Worker concurrency must be 0 (stopped) or 1 (serial paper execution)."
  }
}
