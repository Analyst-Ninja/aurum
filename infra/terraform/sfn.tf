# Three state machines, three crons, one alert path.
#
# The task definitions in tasks.tf already encode a complete logical workflow in their
# `command` — market ingest runs both Yahoo feeds, EDGAR truncates and loads all six
# statement configs, dbt seeds and builds, train runs the whole eight-step modelling loop.
# So each state machine is one or two states, not the ten the original plan described.
#
# The trade: a failure at modelling step 7 of 8 restarts from step 1, and diagnosis means
# reading the CloudWatch log stream rather than seeing which box went red. What it buys is
# one container start instead of ten (~40 s of PROVISIONING plus an image pull each), and
# no cross-container state juggling — steps 3, 5 and 7 of the modelling loop pass a
# generated `<config>_narrow.yaml` between them through a path derived from the base
# config, which is trivial inside one container and the reason the base config had to move
# onto EFS in the first place.
#
# `sh -c "a && b && c"` stops at the first non-zero exit, which is exactly the semantics
# the ten-state chain was hand-building with `Next`.
#
# dbt and train stay separate states on purpose. A dbt failure must never reach train —
# training on a half-built mart_features produces a model that looks fine and is not — and
# there is no reason to start a 4 vCPU / 16 GB task to discover dbt is broken.

locals {
  # Without AssignPublicIp there is no route to ECR: this deployment has an internet
  # gateway and no NAT, so a task without a public address sits in PROVISIONING until it
  # times out.
  sfn_network = {
    AwsvpcConfiguration = {
      Subnets        = data.aws_subnets.default.ids
      SecurityGroups = [aws_security_group.tasks.id]
      AssignPublicIp = "ENABLED"
    }
  }

  # On-demand Fargate throughout, not Spot. The plan proposed Spot for ingest and dbt;
  # total Fargate spend is under a dollar a month against a ~$122 bill, so Spot saves
  # cents and adds an interruption failure mode to a state that is waiting synchronously.
  sfn_retry = [{
    ErrorEquals     = ["States.TaskFailed"]
    IntervalSeconds = 60
    MaxAttempts     = 2
    BackoffRate     = 2.0
  }]

  sfn_catch = [{
    ErrorEquals = ["States.ALL"]
    Next        = "NotifyFailure"
    ResultPath  = "$.error"
  }]

  sfn_task_states = {
    ingest_market = {
      arn     = aws_ecs_task_definition.ingest_market.arn
      timeout = 5400
      retry   = local.sfn_retry
    }
    ingest_edgar = {
      arn     = aws_ecs_task_definition.ingest_edgar.arn
      timeout = 5400
      # One attempt only. A retry re-truncates and re-pulls ~27 minutes of SEC traffic,
      # and the failures this job actually sees (a 403, a schema change) do not clear on
      # their own.
      retry = [{
        ErrorEquals     = ["States.TaskFailed"]
        IntervalSeconds = 60
        MaxAttempts     = 1
        BackoffRate     = 2.0
      }]
    }
    dbt = {
      arn     = aws_ecs_task_definition.dbt.arn
      timeout = 5400
      retry   = local.sfn_retry
    }
    train = {
      arn     = aws_ecs_task_definition.train.arn
      timeout = 10800
      # No retry. A one-hour fit that OOMs or errors does not succeed on a blind second
      # attempt, and this is the most expensive task in the account.
      retry = []
    }
  }

  # Note there are no ContainerOverrides: the task definition's own `command` IS the
  # workflow. Overriding it here would put the pipeline in two places at once.
  sfn_run_task_parameters = {
    for key, task in local.sfn_task_states : key => {
      Cluster              = aws_ecs_cluster.aurum.arn
      TaskDefinition       = task.arn
      LaunchType           = "FARGATE"
      NetworkConfiguration = local.sfn_network
    }
  }

  # A Catch lands here with the error under $.error, publishes it, then fails the
  # execution — so a broken job produces BOTH an email and a red execution, rather than a
  # green one that quietly emailed someone.
  sfn_notify_failure = {
    for machine in ["daily-market", "semimonthly-edgar", "monthly-train"] :
    machine => {
      Type     = "Task"
      Resource = "arn:aws:states:::sns:publish"
      Parameters = {
        TopicArn = aws_sns_topic.alerts.arn
        Subject  = "AURUM: ${var.project}-${machine} FAILED"
        # Intrinsic-function arguments are a single-quoted literal, so this stays on one
        # line; the Cause already carries the ECS task ARN and the container exit code.
        "Message.$" = "States.Format('${var.project}-${machine} failed. Error: {} Cause: {} Logs: CloudWatch /ecs/${var.project}', $.error.Error, $.error.Cause)"
      }
      Next = "Failed"
    }
  }
}

# --- IAM ----------------------------------------------------------------------------

data "aws_iam_policy_document" "sfn_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.project}-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume_role.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    sid     = "RunTaskDefinitions"
    actions = ["ecs:RunTask"]
    resources = [
      aws_ecs_task_definition.ingest_market.arn,
      aws_ecs_task_definition.ingest_edgar.arn,
      aws_ecs_task_definition.dbt.arn,
      aws_ecs_task_definition.train.arn,
    ]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.aurum.arn]
    }
  }

  # Task ARNs are generated at run time, so these cannot be scoped to a resource.
  statement {
    sid       = "ManageRunningTasks"
    actions   = ["ecs:StopTask", "ecs:DescribeTasks"]
    resources = ["*"]
  }

  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.task.arn,
    ]
  }

  # Required by the `.sync` integration pattern, which subscribes to ECS task state
  # changes through a managed EventBridge rule. Missing this is the most common reason a
  # runTask.sync state fails with AccessDeniedException before the task ever starts.
  statement {
    sid     = "ManagedEventsRuleForRunTaskSync"
    actions = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = [
      "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule",
    ]
  }

  statement {
    sid       = "PublishFailureAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "${var.project}-sfn"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "StartScheduledExecutions"
    actions = ["states:StartExecution"]
    resources = [
      aws_sfn_state_machine.daily_market.arn,
      aws_sfn_state_machine.semimonthly_edgar.arn,
      aws_sfn_state_machine.monthly_train.arn,
    ]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.project}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# --- State machines -----------------------------------------------------------------
#
# Every ECS state uses `arn:aws:states:::ecs:runTask.sync`, which waits for the task and
# fails the state on a non-zero container exit. That makes GH-72's exit-code fix
# load-bearing — src/ingestion/cli.py:47 turns a swallowed feed exception into exit 1,
# without which a broken feed would record a green execution.

resource "aws_sfn_state_machine" "daily_market" {
  name     = "${var.project}-daily-market"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "Weeknight market ingest, then the warehouse build that depends on it."
    StartAt = "IngestMarket"
    States = {
      IngestMarket = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["ingest_market"]
        TimeoutSeconds = local.sfn_task_states.ingest_market.timeout
        Retry          = local.sfn_task_states.ingest_market.retry
        Catch          = local.sfn_catch
        ResultPath     = null
        Next           = "Dbt"
      }
      Dbt = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["dbt"]
        TimeoutSeconds = local.sfn_task_states.dbt.timeout
        Retry          = local.sfn_task_states.dbt.retry
        Catch          = local.sfn_catch
        ResultPath     = null
        End            = true
      }
      NotifyFailure = local.sfn_notify_failure["daily-market"]
      Failed        = { Type = "Fail" }
    }
  })
}

resource "aws_sfn_state_machine" "semimonthly_edgar" {
  name     = "${var.project}-semimonthly-edgar"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "Market prices, then EDGAR financial statements, then the warehouse build over both."
    StartAt = "IngestMarket"
    States = {
      # Prices first, fundamentals second, build last. The silver and intermediate models
      # join fundamentals onto the price panel, so building with fresh statements against
      # a stale price history would produce a mart whose two halves are as-of different
      # dates. Ingesting both before the single build keeps them aligned.
      #
      # This duplicates the 22:30 market ingest on the 1st and 15th, harmlessly: both feeds
      # resume from `SELECT MAX(date) GROUP BY symbol`, so the later run simply picks up
      # whatever the earlier one did not. On a weekend the feeds return SUCCESS_NO_DATA,
      # which is not a failure.
      IngestMarket = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["ingest_market"]
        TimeoutSeconds = local.sfn_task_states.ingest_market.timeout
        Retry          = local.sfn_task_states.ingest_market.retry
        Catch          = local.sfn_catch
        ResultPath     = null
        Next           = "IngestEdgar"
      }
      IngestEdgar = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["ingest_edgar"]
        TimeoutSeconds = local.sfn_task_states.ingest_edgar.timeout
        Retry          = local.sfn_task_states.ingest_edgar.retry
        Catch          = local.sfn_catch
        ResultPath     = null
        Next           = "Dbt"
      }
      # The same aurum-dbt task definition the daily machine uses — seed, build, 237 tests.
      # Not a reduced `dbt run`: this is the build that lands two weeks of fundamentals, so
      # it is the one that most needs the tests.
      #
      # It has to live in this machine rather than borrowing the daily one: this cron fires
      # on any day of the week and the daily machine is MON-FRI, so fundamentals landed on
      # a Saturday the 15th would otherwise not reach gold.mart_features until Monday
      # 22:30 — about 64 hours later.
      Dbt = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["dbt"]
        TimeoutSeconds = local.sfn_task_states.dbt.timeout
        Retry          = local.sfn_task_states.dbt.retry
        Catch          = local.sfn_catch
        ResultPath     = null
        End            = true
      }
      NotifyFailure = local.sfn_notify_failure["semimonthly-edgar"]
      Failed        = { Type = "Fail" }
    }
  })
}

resource "aws_sfn_state_machine" "monthly_train" {
  name     = "${var.project}-monthly-train"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "The full modelling loop: train full, SHAP, retrain narrowed, compare, backtest."
    StartAt = "Train"
    States = {
      Train = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.sync"
        Parameters     = local.sfn_run_task_parameters["train"]
        TimeoutSeconds = local.sfn_task_states.train.timeout
        Catch          = local.sfn_catch
        ResultPath     = null
        End            = true
      }
      NotifyFailure = local.sfn_notify_failure["monthly-train"]
      Failed        = { Type = "Fail" }
    }
  })
}

# --- Schedules ----------------------------------------------------------------------
#
# All three fire on the 1st of a month. They are ordered to follow the data dependency —
# ingest, then the build that consumes it, then the model that reads the build — and do not
# overlap in wall-clock:
#
#   06:00-07:30  EDGAR ingest -> dbt
#   12:00-13:00  train
#   22:30-23:30  market ingest -> dbt
#
# The gap between the EDGAR build and training is deliberate slack: EDGAR takes ~27 minutes
# and the dbt build after it roughly as long again, so 12:00 leaves several hours of margin
# before training reads gold.mart_features.

resource "aws_scheduler_schedule" "daily_market" {
  name       = "${var.project}-daily-market"
  group_name = "default"

  # 22:30 UTC is ~2.5 h after the US close, late enough for Yahoo to have settled the
  # day's bars.
  schedule_expression          = "cron(30 22 ? * MON-FRI *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.daily_market.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "semimonthly_edgar" {
  name       = "${var.project}-semimonthly-edgar"
  group_name = "default"

  schedule_expression          = "cron(0 6 1,15 * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.semimonthly_edgar.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "monthly_train" {
  name       = "${var.project}-monthly-train"
  group_name = "default"

  # 12:00, not 02:00. The 1st also carries the EDGAR run at 06:00 and the warehouse build
  # that follows it, and training reads gold.mart_features — starting at 02:00 would train
  # on fundamentals up to two weeks stale while a fresher set landed four hours later.
  schedule_expression          = "cron(0 12 1 * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.monthly_train.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# --- Alerting and budget ------------------------------------------------------------

# Terraform creates this as `pending confirmation`; the address owner has to click the
# link in the confirmation email once. Until they do, failures are silent.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# The original $20/month ceiling was abandoned on 2026-09-06 when RDS moved to
# db.m7g.large (docs/infra/aws-deployment-plan.md §5). A budget set below the known
# steady-state cost alerts every single month and is therefore a budget nobody reads.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
