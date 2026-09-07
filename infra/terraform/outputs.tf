# Everything GH-74 needs to run a task by hand, and GH-75 needs to wire a state machine.

output "rds_endpoint" {
  description = "RDS hostname. This is the HOST env var the ingestion, dbt and modelling configs read."
  value       = aws_db_instance.aurum.address
}

output "ecr_repository_url" {
  description = "ECR repository URL for `make push`."
  value       = aws_ecr_repository.aurum.repository_url
}

output "efs_id" {
  description = "EFS filesystem holding models/ and data/."
  value       = aws_efs_file_system.artifacts.id
}

output "ecs_cluster_name" {
  description = "ECS cluster name for `aws ecs run-task --cluster`."
  value       = aws_ecs_cluster.aurum.name
}

output "task_security_group_id" {
  description = "Security group for the awsvpcConfiguration of a run-task call."
  value       = aws_security_group.tasks.id
}

output "subnet_ids" {
  description = "Subnets for the awsvpcConfiguration of a run-task call. Pair them with assignPublicIp=ENABLED."
  value       = data.aws_subnets.default.ids
}

output "sns_alerts_topic_arn" {
  description = "Alerts topic. GH-75 subscribes an email to it and points the Step Functions Catch states here."
  value       = aws_sns_topic.alerts.arn
}

output "run_task_network_configuration" {
  description = "Ready-made --network-configuration argument for `aws ecs run-task`."
  value       = "awsvpcConfiguration={subnets=[${join(",", data.aws_subnets.default.ids)}],securityGroups=[${aws_security_group.tasks.id}],assignPublicIp=ENABLED}"
}

output "sfn_daily_market_arn" {
  description = "Weeknight market ingest + dbt build. `aws stepfunctions start-execution --state-machine-arn` this to force a run."
  value       = aws_sfn_state_machine.daily_market.arn
}

output "sfn_semimonthly_edgar_arn" {
  description = "EDGAR truncate-and-reload, 1st and 15th."
  value       = aws_sfn_state_machine.semimonthly_edgar.arn
}

output "sfn_monthly_train_arn" {
  description = "The full modelling loop, 1st of the month."
  value       = aws_sfn_state_machine.monthly_train.arn
}

output "artifacts_bucket" {
  description = "S3 bucket the train task publishes each run to, under runs/<version>/<timestamp>/. Injected into every task as AURUM_ARTIFACTS_BUCKET."
  value       = aws_s3_bucket.artifacts.bucket
}