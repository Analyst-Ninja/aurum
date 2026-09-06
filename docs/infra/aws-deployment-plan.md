# AURUM — AWS Deployment Plan

**Status:** Planned — nothing built yet
**Date:** 2026-09-06
**Related:** [../operations/infra-as-code.md](../operations/infra-as-code.md) ·
[../operations/training-container.md](../operations/training-container.md) ·
[../operations/cicd.md](../operations/cicd.md) ·
[../modeling/pipeline-runbook.md](../modeling/pipeline-runbook.md)

---

## 1. Why

Phase 6 is built: ingestion → the dbt medallion → `gold.mart_features` → LightGBM → backtest all run
end to end. They run on a laptop — Postgres on `localhost`, dbt from `~/.dbt/profiles.yml`, training
in a container with `./models` and `./data` bind-mounted, every run started by hand.

This plan moves that to AWS and puts it on a schedule:

- **RDS Postgres** replaces local Postgres — landing plus bronze/silver/gold in one instance.
- **One image on ECR**, run as **ECS Fargate tasks** for ingestion, dbt and modelling.
- **Step Functions + EventBridge Scheduler** replace manual invocation.
- **Terraform** owns the infrastructure; state in S3, `apply` run locally.

Two constraints shape everything below:

1. **This is a personal project, not a platform.** Every choice takes the smaller option.
2. **Budget ceiling: $20/month.** This drives the network topology and the DB instance class more
   than any technical consideration does.

**Not included:** Kafka/MSK, Snowflake, `src/inference/`, `src/mcp/`, Airflow, model serving,
automatic promotion, multiple environments, Multi-AZ.

## 2. Schedules

| State machine | Cron (UTC) | Steps |
|---|---|---|
| `aurum-daily-market` | `cron(30 22 ? * MON-FRI *)` | `ingest yahoo/ohlcv_1d -f False` |
| `aurum-monthly-edgar` | `cron(0 6 1 * ? *)` | `Map` over the 6 EDGAR configs, `MaxConcurrency: 1` |
| `aurum-weekly-model` | `cron(0 2 ? * SAT *)` | the full modelling loop — 10 states, §2.1 |

dbt runs weekly, before training, in the same state machine — a dbt failure must never reach
`train`. The daily job only ingests.

### 2.1 The weekly chain

Not train-and-backtest. The pipeline in
[`pipeline-runbook.md`](../modeling/pipeline-runbook.md) trains on every feature, ranks
features with SHAP, retrains on the narrowed set, then **compares the two on the holdout**.
That comparison is what says whether the narrowed model is worth migrating to.

The same two task definitions run throughout; only `command` changes.

| # | Task def | Command |
|---|---|---|
| 1 | `aurum-dbt` | `dbt seed` |
| 2 | `aurum-dbt` | `dbt build` |
| 3 | `aurum-train` | `model train -c /app/models/configs/lgbm_xs_excess_5d.yaml --version-suffix full` |
| 4 | `aurum-train` | `model evaluate -c <base> --version latest-full` |
| 5 | `aurum-train` | `model select-features -c <base> --version latest-full` |
| 6 | `aurum-dbt` | `sh -c "cp the seed into the dbt project && dbt seed --select selected_features && dbt build --select mart_feature_summary"` |
| 7 | `aurum-train` | `model train -c ..._narrow.yaml --version-suffix narrow` |
| 8 | `aurum-train` | `model evaluate -c <narrow> --version latest-narrow` |
| 9 | `aurum-train` | `model compare -c <narrow> --version latest-narrow --baseline latest-full` |
| 10 | `aurum-train` | `model backtest -c <narrow> --version latest-narrow` |

Three things make this work, and none of them are obvious:

- **`--version-suffix` is required, not cosmetic.** Both trains run on the same day from the
  same image, so `version_id()` — `{date}-{git short sha}` — produces the *same id* for both,
  and the narrowed run would overwrite the baseline it is meant to be compared against. The
  flag also publishes `models/latest-full` and `models/latest-narrow`, which is how the states
  above name their inputs: ASL has no date formatting, so the state machine cannot rebuild
  `20260906-a3aff7a-narrow` on its own.
- **The base config lives on EFS, not in the image.** `select-features` writes the generated
  `<config>_narrow.yaml` beside the base config, and that path is derived rather than
  configurable. Every state is a fresh container, so a config baked into the image would take
  the generated narrow config down with it when the task exits.
- **State 6 copies the seed into the dbt project directory first.** dbt reads the seed CSV from
  there, not from the config's `seed_path`. And `dbt seed` must precede
  `dbt build --select mart_feature_summary`, because the mart reads the Postgres table rather
  than the CSV.

**Promotion stays manual.** `compare` writes `comparison.json` with a `narrowed_wins` verdict
and promotes nothing, which matches
[training-and-retraining.md](../modeling/training-and-retraining.md) — it treats the two-sided
gate as a judgement call.

Note that `models/latest` **does** move: `save_run` repoints it on every train, so after state 7
it points at the narrowed run whether or not that run won. `latest` means "most recently
trained", not "blessed"; `latest-full` and `latest-narrow` are the names that carry meaning.
The loop also does **not** commit `selected_features.csv` back to git the way the manual runbook
does — it lives on EFS, and you copy it into the repo by hand if you keep the narrowed model.

Weekly runtime is roughly an hour: two fits (~30 min on 193 features, ~5 min on 40) plus SHAP.

## 3. Architecture

```
EventBridge Scheduler (3 crons) → Step Functions (3) ──runTask.sync──▶ ECS Fargate
                                       │ Catch → SNS → email              (default VPC, public
                                                                           subnets, SG no ingress)
                                                                                │
                                          RDS Postgres ◀───────────────────────┤
                                          EFS /app/models, /app/data ◀─────────┤
                                          Yahoo / SEC ◀──── IGW, no NAT ───────┘
```

All four task definitions run the **same image** from one ECR repo, differing only in `command`,
cpu/memory, and which env and secrets they receive.

## 4. What was deliberately not built

| Instead of | Do | Why |
|---|---|---|
| Custom VPC + 4 subnets + IGW + route tables | **Default VPC**, two `data` lookups | ~20 resources → 2 data blocks. The security groups do the real work either way |
| NAT Gateway (**$32/mo**) | Tasks in the default public subnets with `assignPublicIp` | Egress out the IGW is free. `sg-tasks` has zero ingress rules, so the public IP is egress-only in practice |
| 4 interface VPC endpoints (**$29/mo**) | Nothing. Only the free S3 gateway endpoint | They exist to avoid NAT data charges; with no NAT they are pure cost |
| 7 Terraform modules | One flat set of `.tf` files | Modules earn their keep on reuse. There is no reuse |
| A `bootstrap/` Terraform project for the state bucket | Two `aws` CLI commands, documented | A whole sub-project for one bucket |
| DynamoDB lock table | S3 native locking (`use_lockfile = true`, TF ≥ 1.10) | One operator, one laptop |
| Secrets Manager (**$0.40/secret/mo**) | SSM Parameter Store `SecureString` | Free. Costs managed rotation, which nothing here needs |
| Per-layer CloudWatch alarms | One SNS topic + a Step Functions `Catch` | The requirement is "email me when a job breaks" |
| Migrating the local warehouse with `pg_dump` | RDS starts empty; a one-off backfill rebuilds it from source | The sources are still there. The local database stays as the reference copy |

## 5. Cost

| Item | ~USD/mo |
|---|---|
| RDS `db.t4g.micro`, 30 GB gp3, 7-day backups | 15.10 |
| EFS (~5 GB, IA after 7 days) | 1.00 |
| ECR (one image, 3 tags retained) | 0.75 |
| Fargate — daily ingest, monthly EDGAR, weekly dbt + train (Spot where safe) | 0.80 |
| CloudWatch Logs (7-day retention) | 0.50 |
| Public IPv4 hours (billed only while a task runs) | 0.20 |
| Step Functions, Scheduler, SNS, SSM, S3 state | ~0.05 |
| **Total** | **~18.40** |

RDS is 82 % of the bill. Two levers not assumed here: if the AWS account is under 12 months old,
`db.t4g.micro` + 20 GB is **free tier** and the total drops to ~$5; a 1-year no-upfront Reserved
Instance cuts the instance line ~35 %.

### The `db.t4g.micro` trade-off

`db.t4g.micro` is **2 burstable vCPU / 1 GB RAM**. That is small for this warehouse: the silver
intermediate models run window functions over a 900-day lookback per symbol across 503 symbols, and
with 1 GB they spill to disk rather than sorting in memory.

Stated honestly: **it will work, but the one-off full `dbt build` in Part 3 will take hours rather
than minutes.** The weekly incremental build is bounded by `window_rewrite_days: 90` and should stay
in the minutes. Mitigation is `threads: 2` in the container's dbt profile instead of the host
profile's 4.

`instance_class` is a one-line change with a few minutes of downtime, so **start on micro and resize
to `db.t4g.small` if the weekly build hurts.** That is +$12/mo and breaks the ceiling — a decision to
make with real numbers from Part 3, not now.

## 6. Constraints found in the code

These are not preferences; each one breaks the deployment if ignored.

| # | Constraint | Consequence |
|---|---|---|
| 1 | `.dockerignore` excludes `src/transformation/` | The existing image **cannot run dbt**. The exclusion must go |
| 2 | The `dbt` dependency group cannot install under `--no-build` — `dbt-core-experimental-parser` is sdist-only (`pyproject.toml` comment) | The image needs a second `uv sync` layer without `--no-build`. CI's install path stays as it is |
| 3 | `dbt_packages/` is gitignored (`src/transformation/aurum_dwh/.gitignore:5`) | It is not in the build context; `dbt deps` must run at image build time |
| 4 | No `profiles.yml` exists in the repo | The image needs one templated on env vars, kept **outside** the dbt project dir so host runs still use `~/.dbt/profiles.yml` |
| 5 | `models/latest` is a symlink repointed with `os.replace`; `data/training` is a Parquet cache | Both need POSIX semantics → **EFS**, not S3. No registry rewrite |
| 6 | The image carries no `.git` | `AURUM_GIT_SHA` must be injected, or every run stamps `{date}-unknown` and overwrites the previous version |
| 7 | `BaseFeed.run()` swallows exceptions and reports `execution_status: FAILED` in a dict; `run_feed()` discards that dict and `cli.main()` ignores the return | **A failed feed exits 0.** Step Functions would record a green run. Must be fixed before anything is scheduled |
| 8 | Env var names are fixed: `HOST`, `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD`, `SEC_USER_AGENT`, `AURUM_GIT_SHA` | `src/utils/env.py` uses `load_dotenv`, which does not override real process env — so ECS-injected values win with **no code change** |
| 9 | Training needs ≥12 GB; 8 GB is OOM-killed with exit 137 ([training-container.md](../operations/training-container.md) §5) | The train task gets 16 GB. Unrelated to the DB instance size |
| 10 | `seeds/selected_features.csv` is committed | The first training run needs no prior SHAP pass |

`infra-as-code.md` §1 currently records two non-goals this plan contradicts — "no cloud provider (no
AWS/GCP footprint in v2)" and "EDGAR producer host … SEC blocks cloud IPs — this stays a local
process by design". Both are superseded by this document; the EDGAR one carries a verification gate
in Part 3 rather than being waved away.

---

## 7. Part 1 — Image and ingestion exit code

Rename `docker/modeling.Dockerfile` → **`docker/aurum.Dockerfile`**. New `docker/entrypoint.sh` and
`docker/dbt/profiles.yml`. Edit `.dockerignore`, `docker-compose.modeling.yml`,
`src/ingestion/runner.py`, `src/ingestion/cli.py`, `docs/operations/training-container.md`,
`CLAUDE.md`.

- `.dockerignore` — drop the `src/transformation/` line.
- Dockerfile — keep the existing `uv sync --locked --no-build --group modeling --no-install-project`
  layer (fast, cached, mirrors CI), add a second
  `uv sync --locked --group dbt --group modeling --no-install-project` **without** `--no-build`.
  Copy all of `src/` and `docker/`. Run `dbt deps` at build time (constraint 3).
- `ENV DBT_PROFILES_DIR=/app/docker/dbt`. The profile templates `{{ env_var('HOST') }}`,
  `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD`, `dbname: aurum`, `schema: bronze`, `threads: 2`.
- `docker/entrypoint.sh` — dispatch on the first argument: `ingest` → `python -m src.ingestion.cli`,
  `dbt` → `cd` to the project dir then `dbt`, `model` → `python -m src.modeling.cli`. Anything else
  is `exec`'d verbatim so `sh` and `--help` still work.
  `ENTRYPOINT ["/app/docker/entrypoint.sh"]`, `CMD ["model", "--help"]`.
- **Exit code** — `run_feed()` returns the metrics dict; `main()` exits 1 on
  `execution_status == "FAILED"` only. `SUCCESS_NO_DATA` is a market holiday, not a failure.
  `BaseFeed.run()`'s swallowing contract is unchanged. Tests go in a new `tests/ingestion/` package —
  `tests/` currently holds only `tests/modeling/`.
- **Breaking change** — the entrypoint no longer defaults to the modelling CLI, so every documented
  `run --rm trainer train …` becomes `run --rm trainer model train …`. Compose,
  `training-container.md` and the docker block in `CLAUDE.md` are updated in the same change.

**Verify:** build the image; against local Postgres run `dbt debug`, an incremental `ingest`, and a
smoke `model train`. `uv run ruff check src/ main.py` and `uv run pytest tests/ -v` pass.

## 8. Part 2 — Terraform

The state bucket is created once, by hand:

```bash
aws s3 mb s3://aurum-tfstate-<account-id>
aws s3api put-bucket-versioning --bucket aurum-tfstate-<account-id> \
  --versioning-configuration Status=Enabled
```

```
infra/terraform/
├── main.tf        # default VPC + subnets (data), SGs, ECR, EFS + access point, ECS cluster, logs, IAM
├── rds.tf         # instance + subnet group
├── tasks.tf       # 4 task definitions
├── sfn.tf         # 3 state machines, 3 schedules, SNS, budget
├── variables.tf   # region, db_password (sensitive), sec_user_agent (sensitive), alert_email, image_tag
├── outputs.tf
├── versions.tf    # provider pins + S3 backend with use_lockfile = true
└── .gitignore     # *.tfstate*, *.tfvars, .terraform/
```

`.github/workflows/terraform.yml` already path-filters on `infra/terraform/**` and runs `fmt -check`,
`init -backend=false`, `validate` and `tflint` — it starts doing real work with no edit to CI.

- **Network** — `data "aws_vpc" "default"` and `data "aws_subnets"`. Two security groups: `sg-tasks`
  (no ingress, all egress) and `sg-data` (5432 and 2049 from `sg-tasks` only).
- **RDS** — postgres 17, `db.t4g.micro`, 30 GB gp3, `max_allocated_storage = 100` (storage
  autoscaling), `storage_encrypted`, `publicly_accessible = false`, `backup_retention_period = 7`,
  `deletion_protection = true`, `db_name = "aurum"`.
- **SSM `SecureString`** — `/aurum/db_password`, `/aurum/sec_user_agent`.
- **ECR** — repo `aurum`, scan on push, lifecycle keeping the last 3 images.
- **EFS** — encrypted, mount targets in the default subnets, one access point at `uid/gid 1000`
  (matching the image's `aurum` user), `root_directory /aurum`, IA after 7 days.
- **ECS** — cluster `aurum` with `FARGATE` and `FARGATE_SPOT`; log group `/ecs/aurum`, 7-day
  retention; an execution role (task-execution policy + `ssm:GetParameters` scoped to the two
  parameter ARNs) and a task role (EFS mount/write, `sns:Publish`).
- **Task definitions** — one image, EFS mounted at `/app/models` and `/app/data`, env
  `HOST`/`PORT`/`AURUM_GIT_SHA`, secrets pulled from SSM:

  | Task definition | cpu / memory | capacity |
  |---|---|---|
  | `aurum-ingest-market` | 1024 / 2048 | Spot |
  | `aurum-ingest-edgar` | 1024 / 4096 | Spot |
  | `aurum-dbt` | 2048 / 4096 | Spot |
  | `aurum-train` | 4096 / 16384 | On-demand |

  Training stays on-demand so a 30-minute run is not interrupted at minute 28.
- A `Makefile` target: `make push TAG=$(git rev-parse --short HEAD)` — ECR login,
  `docker build --platform linux/amd64`, tag, push. The platform flag is mandatory on Apple silicon.

**Verify:** `terraform fmt -check -recursive`, `terraform validate`, `tflint --recursive`, then apply.

## 9. Part 3 — Push, run each task by hand, backfill

Nothing is scheduled yet. Each task is invoked with `aws ecs run-task` and
`assignPublicIp=ENABLED` — without it the task cannot reach ECR and hangs in `PROVISIONING`, because
there is no NAT.

1. `dbt debug` — proves RDS reachability, the SSM-injected credentials, the profile and the SGs.
2. `ingest yahoo/ohlcv_1d -f False` over a short window — proves egress works with no NAT.
3. **EDGAR smoke gate** — one config, small window. If SEC returns 403, the Fargate address range is
   blocked: keep the monthly EDGAR leg running locally against the RDS endpoint and skip that state
   machine in Part 4. Everything else proceeds either way.
4. **Backfill, attended** — Yahoo full load (503 symbols, 2000 → today, ~2.9 M rows) → the 6 EDGAR
   configs serially, respecting the 10 req/s cap → `dbt seed` → `dbt build` (the slow one; watch
   `FreeStorageSpace` and `CPUCreditBalance`) → first `train --version-suffix full`, `evaluate`,
   `backtest`. Also copy the base config and the seed CSV onto EFS under `/app/models/configs/`
   and `/app/models/seeds/`, which §2.1 depends on.

**Accept when** `gold.mart_features` holds ~2.9 M rows across 503 symbols, `dbt test` gives the
documented **237 tests, 2 warn, 0 error**, and one `models/<date>-<sha>/` exists with `metrics.json`
and `backtest/summary.json` — carrying a real sha, not `unknown`.

The local Postgres is not decommissioned. It stays as the reference copy until a full week of
scheduled runs is green.

## 10. Part 4 — Schedules, alerts, documentation

- Three state machines. Every ECS step uses `arn:aws:states:::ecs:runTask.sync`, which waits for the
  task and fails on a non-zero exit — this is what makes Part 1's exit-code fix load-bearing.
  `Retry` ×2 at 60 s with backoff 2.0; `Catch: ["States.ALL"]` → SNS publish → `Fail`.
- Three `aws_scheduler_schedule` entries, `flexible_time_window { mode = "OFF" }`.
- One SNS topic plus an email subscription. AWS Budgets: $20/month, alerting at 80 % actual.
- Fold the measured results back into this document — real runtimes, real cost, and a troubleshooting
  table (SEC 403, exit 137 on train, `models/latest` missing, dbt profile not found, task stuck in
  `PROVISIONING`, `CPUCreditBalance` at zero).
- Add a row for this doc to [`docs/README.md`](../README.md), and note in
  [`infra-as-code.md`](../operations/infra-as-code.md) §1 that its "no cloud provider" non-goal and
  its "EDGAR stays local" row are superseded here.
- Update `README.md` § Current state and the CLAUDE.md § Project state section.

**Verify:** force one execution of each state machine; then break one deliberately and confirm the
email arrives and the execution ends `FAILED` rather than green.

## 11. Verification

```bash
# infrastructure
cd infra/terraform && terraform fmt -check -recursive && terraform validate && tflint --recursive

# image
docker build --platform linux/amd64 -f docker/aurum.Dockerfile -t aurum:$(git rev-parse --short HEAD) .
docker run --rm aurum:local model --help && docker run --rm aurum:local dbt --version

# repo gates, unchanged
uv run ruff check src/ main.py && uv run pytest tests/ -v

# one task, by hand
aws ecs run-task --cluster aurum --task-definition aurum-dbt --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG_TASKS],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"aurum","command":["dbt","debug"]}]}'

# state machines
aws stepfunctions start-execution --state-machine-arn "$WEEKLY_ARN"

# warehouse correctness, with a local .env pointed at the RDS endpoint
cd src/transformation/aurum_dwh && uv run --group dbt dbt test    # 237 tests, 2 warn, 0 error
psql -c "select count(*), count(distinct symbol), max(date) from gold.mart_features;"
```

**Done when** a full week of daily, weekly and (on the 1st) monthly runs completes green unattended,
`gold.mart_features` advances, a new `models/<date>-<sha>/` appears with `metrics.json` and
`backtest/summary.json`, and month-to-date cost tracks under $20.

## 12. Risks

| Risk | Mitigation |
|---|---|
| 1 GB RDS is too small for the gold build | `threads: 2`; watch `CPUCreditBalance` and `FreeableMemory`; resizing is a one-line change but costs +$12/mo and breaks the ceiling |
| SEC 403s the Fargate address | The Part 3 gate runs before the monthly state machine is built; fall back to running EDGAR locally against RDS |
| Task stuck in `PROVISIONING` | Missing `assignPublicIp=ENABLED` — there is no NAT to fall back on |
| The backfill is far slower than it is locally | Expected, and it is one-off. Run it attended; storage autoscaling to 100 GB prevents a disk-full stall |
| Spot interruption kills a run | Only ingest and dbt run on Spot, both retryable. Training is on-demand |
| The dbt group breaks the image build | The second sync layer drops `--no-build`; CI's install path is untouched |
| A model version stamps as `{date}-unknown` | `AURUM_GIT_SHA = var.image_tag` on the modelling tasks |
| The entrypoint change breaks documented commands | Compose and both docs are updated in the same change as the Dockerfile |
| No DB password rotation (SSM, not Secrets Manager) | Accepted to save $0.40/mo. Rotation is a manual `terraform apply` with a new variable |
