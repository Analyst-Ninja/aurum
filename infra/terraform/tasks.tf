# Four task definitions, one image.
#
# They differ only in size and in the command they pass. The first argument selects the
# workload — `ingest`, `dbt` or `model` — which docker/entrypoint.sh dispatches on (GH-72).
#
# NOTE: assignPublicIp is NOT set here. It belongs to the network configuration supplied at
# run time, by `aws ecs run-task --network-configuration` (GH-74) or by the Step Functions
# state (GH-75). There is no NAT gateway in this deployment, so a task without a public IP
# cannot reach ECR and sits in PROVISIONING until it times out. The
# `run_task_network_configuration` output has the correct value ready to paste.

locals {
  image = "${aws_ecr_repository.aurum.repository_url}:${var.image_tag}"

  # Common to every task. AURUM_GIT_SHA matters more than it looks: the image carries no
  # .git, so without it the model registry stamps every version `{date}-unknown` and two
  # runs on the same day overwrite each other.
  environment = [
    { name = "HOST", value = aws_db_instance.aurum.address },
    { name = "PORT", value = "5432" },
    { name = "AURUM_GIT_SHA", value = var.image_tag },
  ]

  # SEC_USER_AGENT is not EDGAR-only. src/ingestion/datasources/api/yahoo/ohlcv.py calls
  # get_sec_user_agent() in its constructor, because the S&P 500 universe is scraped from
  # Wikipedia — which, like SEC, refuses anonymous traffic. Scoping this to the EDGAR task
  # made the market feed die before it read a single row. Every task gets it.
  secrets = [
    { name = "AURUM_USERNAME", valueFrom = aws_ssm_parameter.db_username.arn },
    { name = "AURUM_PASSWORD", valueFrom = aws_ssm_parameter.db_password.arn },
    { name = "SEC_USER_AGENT", valueFrom = aws_ssm_parameter.sec_user_agent.arn },
  ]

  log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "ecs"
    }
  }

  # Both bind mounts from docker-compose.modeling.yml, now backed by EFS. One volume per
  # access point: sharing a single access point across both paths would make /app/models and
  # /app/data the same directory. The access points supply uid/gid 1000, so the container's
  # non-root user owns what it writes.
  mount_points = [
    { sourceVolume = "models", containerPath = "/app/models", readOnly = false },
    { sourceVolume = "data", containerPath = "/app/data", readOnly = false },
  ]
}

# Daily. Incremental from the watermark — -f False is the whole point, a full load would
# re-pull 2000-to-today every night.
resource "aws_ecs_task_definition" "ingest_market" {
  family                   = "${var.project}-ingest-market"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "models"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.models.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name        = var.project
    image       = local.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets
    mountPoints = local.mount_points
    logConfiguration = merge(local.log_configuration, {
      options = merge(local.log_configuration.options, { "awslogs-stream-prefix" = "ingest-market" })
    })
    command = ["ingest", "-c", "src/ingestion/configs/yahoo/ohlcv_1d.yaml", "-f", "False"]
  }])
}

# Monthly. The command is overridden per invocation — GH-75 maps over the six statement
# configs serially, because EDGAR caps at 10 requests/second.
resource "aws_ecs_task_definition" "ingest_edgar" {
  family                   = "${var.project}-ingest-edgar"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "models"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.models.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name        = var.project
    image       = local.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets
    mountPoints = local.mount_points
    logConfiguration = merge(local.log_configuration, {
      options = merge(local.log_configuration.options, { "awslogs-stream-prefix" = "ingest-edgar" })
    })
    command = ["ingest", "-c", "src/ingestion/configs/edgar/income_statements_quarterly.yaml"]
  }])
}

# Weekly, before training. Command overridden: `dbt seed`, then `dbt build`.
resource "aws_ecs_task_definition" "dbt" {
  family                   = "${var.project}-dbt"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 4096
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "models"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.models.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name        = var.project
    image       = local.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets
    mountPoints = local.mount_points
    logConfiguration = merge(local.log_configuration, {
      options = merge(local.log_configuration.options, { "awslogs-stream-prefix" = "dbt" })
    })
    command = ["dbt", "build"]
  }])
}

# Weekly, after dbt. 16 GB because the training panel is ~2.9M x 228 and 8 GB gets
# OOM-killed with exit 137 (docs/operations/training-container.md §5). This is the one task
# that must NOT run on Spot: an interruption at minute 28 of a 30-minute fit wastes it.
resource "aws_ecs_task_definition" "train" {
  family                   = "${var.project}-train"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "models"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.models.id
        iam             = "ENABLED"
      }
    }
  }

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.artifacts.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name        = var.project
    image       = local.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets
    mountPoints = local.mount_points
    logConfiguration = merge(local.log_configuration, {
      options = merge(local.log_configuration.options, { "awslogs-stream-prefix" = "train" })
    })
    command = ["model", "train", "-c", "src/modeling/configs/lgbm_xs_excess_5d.yaml"]
  }])
}
