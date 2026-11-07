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

variable "enable_forward_runtime" {
  description = "Prepare the isolated issuer-data paper runtime; does not itself enable its schedule."
  type        = bool
  default     = false

  validation {
    condition = (!var.enable_forward_runtime || (
      var.aws_region == "eu-central-1" && var.forward_runtime_hash != null && var.forward_alert_email != null
    ))
    error_message = "The forward runtime requires the reviewed Frankfurt region, sealed runtime hash and private alert email."
  }
}

variable "enable_forward_model_calls" {
  description = "Opt in to the exact reviewed Nova Micro EU profile; application reservations still gate each attempt."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_forward_model_calls || var.enable_forward_runtime
    error_message = "Model IAM cannot be enabled without the reviewed forward runtime."
  }
}

variable "enable_forward_schedule" {
  description = "Activate daily UTC 08:00 paper capture only after controller transfer and verified alert delivery."
  type        = bool
  default     = false

  validation {
    condition = (!var.enable_forward_schedule || (
      var.enable_forward_runtime && var.forward_alert_confirmed && var.worker_reserved_concurrency == 1
    ))
    error_message = "Scheduling requires the forward runtime, confirmed alert delivery and one active serial worker."
  }
}

variable "forward_runtime_hash" {
  description = "Canonical SHA256 digest of the reviewed private runtime.json; worker input must match it."
  type        = string
  default     = null

  validation {
    condition     = var.forward_runtime_hash == null || can(regex("^[0-9a-f]{64}$", var.forward_runtime_hash))
    error_message = "Provide the exact 64-character lowercase runtime digest."
  }
}

variable "forward_alert_email" {
  description = "Private project alert email; keep the value in ignored deployment settings. Subscription requires confirmation."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.forward_alert_email == null || can(regex("^[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$", var.forward_alert_email))
    error_message = "Provide a private valid email address or leave notifications unconfigured."
  }
}

variable "forward_alert_confirmed" {
  description = "Explicit operator acknowledgement after subscription confirmation and one successful controlled delivery test."
  type        = bool
  default     = false
}
