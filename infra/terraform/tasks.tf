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
    { name = "AURUM_ARTIFACTS_BUCKET", value = aws_s3_bucket.artifacts.bucket },
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

  # Modelling configs ship in the image. select-features writes the generated
  # <config>_narrow.yaml *beside* the base config and the seed to the path in
  # `select.seed_path`, so both land inside the image's filesystem — container-local, and
  # that is fine: every state below runs in one container, and step 3 regenerates both
  # from scratch each week. Nothing reads last week's copy, so there is no cross-task
  # state to persist and no EFS hop to bootstrap.
  base_config   = "/app/src/modeling/configs/lgbm_xs_excess_5d.yaml"
  narrow_config = "/app/src/modeling/configs/lgbm_xs_excess_5d_narrow.yaml"
  modeling_cli  = "python -m src.modeling.cli"
  dbt_project   = "/app/src/transformation/aurum_dwh"

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

# Daily. Both market feeds, incremental from their watermarks — -f False is the whole
# point, a full load would re-pull 2000-to-today every night.
#
# Sequential, not parallel. Both feeds hit Yahoo, and the configs already self-throttle
# (batch_size 100, sleep_seconds 1); running them concurrently would double the request
# rate for no useful gain. The `&&` also means a 1d failure stops the 1m run rather than
# burying it in the same log.
#
# Sized for the daily increments, not for a first full load. Backfilling either feed needs
# a task-level override — 1024/2048 was OOM-killed (exit 137) doing the 1d history, and
# the first 1m load is ~5.9M rows (503 symbols x ~30 days of retention x 390 minutes).
resource "aws_ecs_task_definition" "ingest_market" {
  family                   = "${var.project}-ingest-market"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 8192
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
    command = ["sh", "-c", join(" && ", [
      "python -m src.ingestion.cli -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False",
      "python -m src.ingestion.cli -c src/ingestion/configs/yahoo/ohlcv_1min.yaml -f False",
    ])]
  }])
}

# 1st and 15th. All six statement configs in one task, sequentially.
#
# A task definition's `command` is only a default — `run-task --overrides` and Step
# Functions `containerOverrides` replace it wholesale — but a default that ingests one of
# six statement types is a trap: running this definition unqualified would silently load
# income_stmts_quarterly and nothing else.
#
# Serial, and deliberately not parallel. SEC caps at 10 requests/second and the limiter is
# per-process, so six concurrent configs would issue roughly six times the intended rate.
# All six take ~27 minutes together.
#
# Truncate-then-load, one table at a time. This re-pulls the full history every run and
# the sink appends: the EDGAR configs carry no watermark_group_by/watermark_date_column,
# and their cols_for_pk is (SYMBOL, QTR, CONCEPT) with no date column at all, so `-f False`
# has nothing to resume from. PostgresDataSource.write_data uses to_sql(if_exists="append")
# with no unique index on MD5_HASH, so without the truncate a second run duplicates every
# row — ~1.9M each time, growing linearly (GH-75).
#
# Truncating makes the landing table match the contract the config already declares:
# `full_load: true` means one full snapshot per run.
#
# Per config rather than all six up front, deliberately. A failure partway through then
# leaves at most ONE table empty until the next run, instead of all six — and the daily
# dbt build at 22:30 would otherwise turn a 06:00 EDGAR failure into a mart with no
# fundamentals at all.
#
# Both steps are gated on freshness (`min_refresh_gap_days: 10` in each EDGAR config): if
# the landing table's newest RUN_DATE is under 10 days old the truncate and the load both
# no-op and exit 0, so a rerun — or the 15th landing close behind the 1st — costs a single
# MAX() query instead of ~27 minutes of SEC traffic. The gate covers the truncate too
# because truncating resets the value it measures.
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
    command = ["sh", "-c", join(" && ", flatten([
      for config in [
        "income_statements_quarterly",
        "income_statements_yearly",
        "balance_sheet_statements_quarterly",
        "balance_sheet_statements_yearly",
        "cashflow_statements_quarterly",
        "cashflow_statements_yearly",
        ] : [
        "python -m src.ingestion.truncate -c src/ingestion/configs/edgar/${config}.yaml",
        "python -m src.ingestion.cli -c src/ingestion/configs/edgar/${config}.yaml",
      ]
    ]))]
  }])
}

# Runs after EVERY ingest — every weeknight behind the market feeds, and again on the 1st
# and 15th behind EDGAR. gold.mart_features is only as fresh as the last build and
# everything downstream reads it, so this rides the ingest state machines rather than
# sitting in front of the monthly training run.
#
# Seed first, then build: three seeds feed models downstream, and mart_feature_summary reads
# the seeded Postgres table rather than the CSV.
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
    command = ["sh", "-c", "cd ${local.dbt_project} && dbt seed && dbt build"]
  }])
}

# Monthly, on the 1st at 12:00 UTC — after that morning's EDGAR ingest and the warehouse
# build behind it, so the fit sees the freshest fundamentals. The whole modelling loop, in the order
# docs/modeling/pipeline-runbook.md
# describes: train on every feature, evaluate, rank features with SHAP, push the ranking into
# the warehouse, retrain narrowed, evaluate, compare the two on the holdout, backtest.
#
# It reads gold.mart_features, which the 06:00 EDGAR leg rebuilt that same morning
# (aws-deployment-plan.md §2.1).
#
# The comparison is the point. Feature selection is a hypothesis, not an improvement — the
# narrowed model has to match or beat the full one on holdout ICIR *and* decile spread. That
# verdict lands in comparison.json; nothing is promoted on the strength of it, because
# promotion stays a human decision (docs/modeling/training-and-retraining.md).
#
# --version-suffix is required, not cosmetic (GH-78): both fits run on the same day from the
# same image, so without it they share a version id and the narrowed run overwrites the
# baseline it is meant to be measured against. The suffix also publishes models/latest-full
# and models/latest-narrow, which is how the later steps name their inputs.
#
# 8 vCPU / 32 GB. LightGBM's histogram build is threaded, so the fit scales with cores and
# this is the one task in the deployment where more vCPU actually buys wall time — the
# ingest tasks are network-bound on Yahoo and SEC, and the dbt task is a thin client whose
# work happens inside Postgres.
#
# Memory was 16 GB, chosen because 8 GB was OOM-killed with exit 137
# (docs/operations/training-container.md §5). Doubled alongside the cores: TreeSHAP over a
# ~2.9M x 228 panel is the memory peak of the run, not the fit, and 8 vCPU on Fargate
# cannot be requested with less than 16 GB anyway.
#
# The cost of this is rounding error — the task runs about an hour a month, so ~$0.47.
#
# Its Step Functions state carries no Retry: a one-hour fit that OOMs does not succeed on
# a blind second attempt, and this is the most expensive task in the account.
resource "aws_ecs_task_definition" "train" {
  family                   = "${var.project}-train"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 8192
  memory                   = 32768
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
    name      = var.project
    image     = local.image
    essential = true
    # LightGBM's own thread count, not the task's. src/modeling/config.py defaults it to
    # 4, tuned for an Apple Silicon laptop where the efficiency cores drag every boosting
    # barrier. Fargate vCPUs are homogeneous, so this must track `cpu` above, or the task
    # pays for 8 and uses 4.
    environment = concat(local.environment, [
      { name = "AURUM_NUM_THREADS", value = "8" },
    ])
    secrets     = local.secrets
    mountPoints = local.mount_points
    logConfiguration = merge(local.log_configuration, {
      options = merge(local.log_configuration.options, { "awslogs-stream-prefix" = "train" })
    })
    command = ["sh", "-c", join(" && ", [
      # 1. Fit on every feature the deny-lists leave.
      "${local.modeling_cli} train -c ${local.base_config} --version-suffix full",
      # 2. Does it rank? IC, ICIR, decile spread against four baselines.
      "${local.modeling_cli} evaluate -c ${local.base_config} --version latest-full",
      # 3. TreeSHAP ranking -> selected_features.csv + the generated narrow config.
      "${local.modeling_cli} select-features -c ${local.base_config} --version latest-full",
      # 4. Push the ranking into the warehouse. A subshell so the cd does not leak into
      #    the later steps, and seed before build because the mart reads the table.
      #    No copy first: `select.seed_path` already points select-features at this
      #    directory, so the CSV dbt seeds is the one step 3 just wrote.
      "(cd ${local.dbt_project} && dbt seed --select selected_features && dbt build --select mart_feature_summary)",
      # 5. Refit on the ~40 survivors. Same command as step 1, different config.
      "${local.modeling_cli} train -c ${local.narrow_config} --version-suffix narrow",
      # 6. Same metrics, so the two are comparable.
      "${local.modeling_cli} evaluate -c ${local.narrow_config} --version latest-narrow",
      # 7. The gate: writes comparison.json with a narrowed_wins verdict. Reports only.
      "${local.modeling_cli} compare -c ${local.narrow_config} --version latest-narrow --baseline latest-full",
      # 8. Does it make money? Overlapping tranches, cost sweep, randomization checks.
      "${local.modeling_cli} backtest -c ${local.narrow_config} --version latest-narrow",
      # 9. Publish both runs to S3 under runs/<version>/<timestamp>/, so the reports and
      #    metrics can be read from a browser instead of by starting a task to cat a file
      #    off EFS. Last on purpose: everything above has already landed on EFS, so an S3
      #    outage costs the publish and not the month's training. Still `&&`-joined, so
      #    the execution ends red and the SNS Catch fires rather than failing silently.
      "${local.modeling_cli} export -c ${local.base_config} --version latest-full",
      "${local.modeling_cli} export -c ${local.narrow_config} --version latest-narrow",
    ])]
  }])
}
