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
  description = "RDS instance class. db.t4g.micro is 1 GB of RAM and is the reason the container dbt profile uses threads: 2 — see docs/infra/aws-deployment-plan.md §5. Resizing to db.t4g.small costs ~$12/mo more and breaks the $20 ceiling."
  type        = string
  default     = "db.t4g.micro"
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
