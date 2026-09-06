# Network, container registry, shared filesystem, cluster, secrets and IAM.
#
# There is no VPC in here on purpose. The default VPC already provides subnets with an
# internet gateway, and the security groups below do the actual isolation — building a
# custom VPC would add ~20 resources and change nothing about who can reach what.
# See docs/infra/aws-deployment-plan.md §4.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# --- Security groups ---------------------------------------------------------------
#
# Tasks run in the default (public) subnets with a public IP, because there is no NAT
# gateway — that line alone would be $32/month against a $20 budget. The public address is
# egress-only in practice: this group has no ingress rules at all, so nothing on the
# internet can open a connection to a task.

resource "aws_security_group" "tasks" {
  name        = "${var.project}-tasks"
  description = "ECS tasks: egress only, no ingress"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "All outbound: ECR, CloudWatch, SSM, Yahoo, SEC EDGAR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-tasks" }
}

# RDS and EFS accept traffic from the task group and from nowhere else.
resource "aws_security_group" "data" {
  name        = "${var.project}-data"
  description = "RDS and EFS: reachable only from the ECS tasks security group"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "Postgres from ECS tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  ingress {
    description     = "NFS from ECS tasks"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  tags = { Name = "${var.project}-data" }
}

# --- Container registry ------------------------------------------------------------

resource "aws_ecr_repository" "aurum" {
  name                 = var.project
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# The image carries the ~400 MB ML stack, so keeping 20 tags would cost more than the
# rest of the non-database footprint combined. Three is enough to roll back.
resource "aws_ecr_lifecycle_policy" "keep_last_three" {
  repository = aws_ecr_repository.aurum.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}

# --- Shared filesystem -------------------------------------------------------------
#
# EFS rather than S3 because the model registry needs POSIX semantics: `models/latest` is
# a symlink repointed with os.replace(). S3 has neither. Rewriting the registry was the
# alternative and it is more work than mounting a filesystem.

resource "aws_efs_file_system" "artifacts" {
  creation_token = "${var.project}-artifacts"
  encrypted      = true

  # The Parquet training cache is read once a week; IA is ~5% the price of standard.
  lifecycle_policy {
    transition_to_ia = "AFTER_7_DAYS"
  }

  tags = { Name = "${var.project}-artifacts" }
}

resource "aws_efs_mount_target" "artifacts" {
  for_each = toset(data.aws_subnets.default.ids)

  file_system_id  = aws_efs_file_system.artifacts.id
  subnet_id       = each.value
  security_groups = [aws_security_group.data.id]
}

# Two access points, not one. The container mounts /app/models and /app/data, and a single
# access point mounted at both paths would make them two views of the SAME directory — the
# model registry and the Parquet training cache written on top of each other. Each gets its
# own subdirectory of the filesystem instead.
#
# uid/gid 1000 matches the non-root `aurum` user in docker/aurum.Dockerfile. Mismatch here
# means the container cannot write its own artifacts.
resource "aws_efs_access_point" "models" {
  file_system_id = aws_efs_file_system.artifacts.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/aurum/models"

    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = { Name = "${var.project}-models" }
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.artifacts.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/aurum/data"

    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = { Name = "${var.project}-data" }
}

# --- Cluster and logs --------------------------------------------------------------

resource "aws_ecs_cluster" "aurum" {
  name = var.project

  # Container Insights bills per metric and this cluster runs four scheduled jobs.
  # CloudWatch Logs plus the Step Functions execution history is enough to debug them.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "aurum" {
  cluster_name       = aws_ecs_cluster.aurum.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${var.project}"
  retention_in_days = var.log_retention_days
}

# --- Secrets -----------------------------------------------------------------------
#
# SSM Parameter Store, not Secrets Manager: SecureString parameters are free where Secrets
# Manager is $0.40/secret/month. The trade is managed rotation, which a single-operator
# project does not use. Rotating means changing the tfvars value and re-applying.

resource "aws_ssm_parameter" "db_username" {
  name  = "/${var.project}/db_username"
  type  = "String"
  value = var.db_username
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/${var.project}/db_password"
  type  = "SecureString"
  value = var.db_password
}

resource "aws_ssm_parameter" "sec_user_agent" {
  name  = "/${var.project}/sec_user_agent"
  type  = "SecureString"
  value = var.sec_user_agent
}

# --- Alerts ------------------------------------------------------------------------
#
# The topic lives here because the task role's sns:Publish has to be scoped to an ARN.
# GH-75 adds the email subscription, the Step Functions Catch states and the alarms.

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

# --- IAM ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what the ECS agent needs to START a task — pull the image, write logs,
# and resolve the `secrets` block. Not what the application code uses.
resource "aws_iam_role" "execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid     = "ReadTaskSecrets"
    actions = ["ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.db_username.arn,
      aws_ssm_parameter.db_password.arn,
      aws_ssm_parameter.sec_user_agent.arn,
    ]
  }

  # SecureString parameters are decrypted with the account's default SSM key.
  statement {
    sid       = "DecryptSecureStrings"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.project}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Task role: what the application code itself may do at run time.
resource "aws_iam_role" "task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "MountArtifactsFilesystem"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.artifacts.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values = [
        aws_efs_access_point.models.arn,
        aws_efs_access_point.data.arn,
      ]
    }
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.project}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
