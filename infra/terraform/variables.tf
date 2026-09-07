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
  description = "ECR image tag the task definitions run — the short SHA `make push` printed. Also injected as AURUM_GIT_SHA, so the model registry stamps a real commit instead of `unknown`. No default on purpose: the repository is IMMUTABLE, so a floating tag like `latest` can never be re-pointed and never exists, and a task definition referencing one fails the pull with `CannotPullContainerError ... not found`. Pass it explicitly: `terraform apply -var=\"image_tag=$(git rev-parse --short HEAD)\"`."
  type        = string

  validation {
    condition     = var.image_tag != "latest"
    error_message = "image_tag cannot be \"latest\": aws_ecr_repository.aurum is IMMUTABLE, so that tag is never pushed and the task pull fails with CannotPullContainerError. Use the short SHA `make push` printed."
  }
}

variable "db_instance_class" {
  description = "RDS instance class. Was db.t4g.micro; its CPU credit balance hit zero during the first backfill and the instance throttled to baseline, stalling both ingestion and dbt. m7g is non-burstable, so there are no credits to exhaust. This is the single largest line in the bill (~$118/mo, against ~$49 for db.t4g.medium) — see docs/infra/aws-deployment-plan.md §5. The default deliberately matches the decision recorded there rather than a cheaper burstable class: the default is what a destroy-and-recreate falls back to, and on 2026-09-06 that silently reverted a resize made by hand."
  type        = string
  default     = "db.m7g.large"
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
  description = "gp3 storage in GB. A floor, not a budget — but do not rely on autoscaling to cover the gap. 30 GB was not enough: the gold build filled it 18 GB -> 0 in ~35 minutes and `dbt build` died with `could not extend file ... No space left on device` on mart_training_set (2026-09-06). Storage autoscaling needs free space under 10% *sustained* and holds a 6-hour cooldown between scalings, so it cannot win that race. mart_features and mart_training_set are ~2.9M x 228 each and coexist during the build, alongside per-session temp sort/hash spill files — four of them at dbt threads: 4. NOTE: RDS storage can only ever grow; this cannot be lowered later without a dump and restore."
  type        = number
  default     = 100
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

variable "artifact_retention_days" {
  description = "How long a run's artifacts live in the S3 artifacts bucket before expiring. Every monthly execution publishes two runs (full and narrow), each carrying a model.txt, so this is the only thing keeping the bucket from growing without bound. Set to 0 to keep objects forever."
  type        = number
  default     = 365
}

variable "github_repository" {
  description = "The owner/repo the GitHub Actions OIDC trust policy is scoped to. Only workflow runs on this repository's main branch can assume aws_iam_role.github_actions."
  type        = string
  default     = "Analyst-Ninja/aurum"
}
