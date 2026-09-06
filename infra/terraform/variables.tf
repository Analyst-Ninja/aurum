variable "region" {
  description = "AWS region for every resource in this configuration."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource, and the value of the Project tag."
  type        = string
  default     = "aurum"
}

variable "db_username" {
  description = "RDS master username. The application connects as this user; a least-privilege role is follow-up work."
  type        = string
  default     = "aurum"
}

variable "db_password" {
  description = "RDS master password. Written to SSM as a SecureString and never committed — set it in terraform.tfvars, which is gitignored."
  type        = string
  sensitive   = true
}

variable "sec_user_agent" {
  description = "Honest User-Agent for SEC EDGAR (name + email). EDGAR returns 403 without one and caps requests at 10/s."
  type        = string
  sensitive   = true
}

variable "image_tag" {
  description = "ECR image tag the task definitions run, normally `git rev-parse --short HEAD`. Also injected as AURUM_GIT_SHA so the model registry stamps a real commit instead of `unknown`."
  type        = string
  default     = "latest"
}

variable "db_instance_class" {
  description = "RDS instance class. Was db.t4g.micro; its CPU credit balance hit zero during the first backfill and the instance throttled to baseline, stalling both ingestion and dbt. m7g is non-burstable, so there are no credits to exhaust. This is the single largest line in the bill — see docs/infra/aws-deployment-plan.md §5."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_publicly_accessible" {
  description = "Give RDS a public IP. Only meaningful alongside db_ingress_cidrs — the security group still decides who may connect."
  type        = bool
  default     = true
}

variable "db_ingress_cidrs" {
  description = "CIDRs allowed to reach Postgres from outside the VPC, on top of the ECS task security group. 0.0.0.0/0 exposes the database to the whole internet; the master password is then the only control in front of it."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GB. Autoscales to db_max_allocated_storage, so this is a floor, not a budget."
  type        = number
  default     = 30
}

variable "db_max_allocated_storage" {
  description = "Ceiling for RDS storage autoscaling. Prevents a disk-full stall during the one-off backfill in GH-74."
  type        = number
  default     = 100
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for /ecs/aurum."
  type        = number
  default     = 7
}

variable "alert_email" {
  description = "Address the Step Functions Catch states and the budget alarms publish to. No default on purpose — an unset address means failures are silent. The SNS subscription lands as `pending confirmation`; the link in the confirmation email has to be clicked once."
  type        = string
  default     = "r.kumar01@hotmail.com"
}

variable "monthly_budget_usd" {
  description = "AWS Budgets ceiling, alerting at 80% actual and 100% forecasted. Defaults to 150, not the 20 the original plan carried: that ceiling was abandoned on 2026-09-06 when RDS moved to db.m7g.large, and a budget below steady-state cost alerts every month and gets ignored."
  type        = number
  default     = 150
}
