# AURUM — Infrastructure as Code (Terraform)

**Version:** 2.0
**Updated:** 2026-10-25
**Status:** Built and deployed. Rewritten from the 2026-07-12 design, which planned Snowflake, Kafka and
Postgres modules that were never built (kept short in §8).
**Related:** [`../infra/aws-deployment-plan.md`](../infra/aws-deployment-plan.md) (what runs and why) ·
[`cicd.md`](cicd.md) (how it ships) · [`../security/risks.md`](../security/risks.md)

---

## 1. What Terraform manages

Everything AURUM runs on, in AWS `us-east-1`, in two independent roots:

| Root | Path | State key | Purpose |
|---|---|---|---|
| Main stack | `infra/terraform/` | `aurum/terraform.tfstate` | the whole footprint below |
| Bootstrap | `infra/terraform/bootstrap/` | `aurum/bootstrap.tfstate` | GitHub OIDC provider + the `aurum-github-actions` role |

The bootstrap root is separate on purpose: destroying the main stack must never delete the role the
destroy run is using.

State lives in S3 bucket `aurum-tfstate-851459781998` (versioned, encrypted) with native locking
(`use_lockfile`, Terraform >= 1.10) — no DynamoDB table. The provider is `hashicorp/aws ~> 6.0`, and every
resource gets default tags `Project=aurum`, `ManagedBy=terraform`.

Not managed by Terraform: the state bucket itself (created by `scripts/deploy.sh bootstrap`), the **MCP
read-only Postgres role** (the AWS provider has no Postgres-role resource and CI cannot reach RDS; see
[`cicd.md`](cicd.md) §3), and table DDL, which belongs to ingestion and dbt.

## 2. File map (`infra/terraform/`)

| File | Contents |
|---|---|
| `versions.tf` | Terraform and provider pins, S3 backend, default tags |
| `variables.tf` | Every input and default (see §4) |
| `main.tf` | Default VPC and subnets, security groups, ECR repo + lifecycle policy, EFS + access points, S3 artifacts bucket, ECS cluster, CloudWatch log group, SSM parameters, SNS topic + email subscription, budget |
| `rds.tf` | Postgres 17 instance, subnet group, parameter group |
| `tasks.tf` | IAM roles and the four ECS task definitions (`aurum-ingest-market`, `aurum-ingest-edgar`, `aurum-dbt`, `aurum-train`) |
| `sfn.tf` | The three Step Functions state machines, their IAM role, and the three EventBridge Scheduler schedules |
| `mcp_host.tf`, `mcp_host.tftpl` | The optional MCP host: `t3.micro` EC2, instance profile, security group, systemd unit (`mcp_enabled`) |
| `outputs.tf` | Endpoints and ready-made commands, including `mcp_port_forward_command` |
| `terraform.tfvars.example` | Template for the gitignored `terraform.tfvars` (local deploys only) |
| `bootstrap/` | `github_oidc.tf`, `variables.tf`, `outputs.tf`, `versions.tf` |

## 3. Resources

- **Network.** The default VPC, one subnet per AZ, no NAT: tasks take a public IP. Two security groups —
  `tasks` (no ingress) and `data` (5432 from the tasks and the MCP host, 2049 from the tasks, plus a rule from
  `db_ingress_cidrs`).
- **RDS.** Postgres 17, `db.m7g.large`, 100 GB gp3, encrypted, single-AZ, 7-day backups, publicly accessible by
  default, deletion protection tied to `allow_destroy`. A parameter group sets `work_mem`,
  `maintenance_work_mem`, `random_page_cost`. The non-burstable class is deliberate: a `t4g.micro` ran out of CPU
  credits during the first backfill.
- **ECR.** One repo, `aurum`, `IMMUTABLE`, scan on push. Lifecycle keeps the last 3 `mcp-` images and the last 3
  of everything else.
- **ECS.** Cluster `aurum` (Fargate), four task definitions that share one image and mount EFS at `/app/models`
  and `/app/data`. Logs go to `/ecs/aurum` (7-day retention).

  | Task | vCPU / memory | Command |
  |---|---|---|
  | `aurum-ingest-market` | 2 / 8 GB | `ohlcv_1d` then `ohlcv_1min`, `-f False` |
  | `aurum-ingest-edgar` | 1 / 4 GB | six EDGAR configs, each truncate then load |
  | `aurum-dbt` | 2 / 4 GB | `dbt seed && dbt build` |
  | `aurum-train` | 8 / 32 GB | train, evaluate, select-features, dbt seed + narrow mart, narrowed train, evaluate, compare, backtest, export x2 |
- **Step Functions + Scheduler.** `aurum-daily-market` (`cron(30 22 ? * MON-FRI *)`), `aurum-semimonthly-edgar`
  (`cron(0 6 1,15 * ? *)`), `aurum-monthly-train` (`cron(0 12 1 * ? *)`), all UTC. Each ECS state is
  `ecs:runTask.sync`; a Catch publishes to SNS and then ends in a `Fail` state. Retries: market and dbt 2, EDGAR 1,
  train none. Timeouts 5400 s, train 10800 s. Details and rationale:
  [`../infra/aws-deployment-plan.md`](../infra/aws-deployment-plan.md).
- **EFS.** Encrypted, IA after 7 days, access points `/aurum/models` and `/aurum/data` (uid/gid 1000), a mount
  target per AZ.
- **S3.** `aurum-artifacts-<account>`: private, AES256, 365-day expiry. The task role can only `PutObject`
  (the `export` step); the MCP host can read `runs/*`.
- **Secrets.** SSM parameters `db_username`, `db_password`, `sec_user_agent`, and with the MCP host
  `mcp_username`, `mcp_password`.
- **Alerts and cost.** SNS topic `aurum-alerts` with an email subscription (confirm the first email), and a
  monthly budget (`monthly_budget_usd`, default 150). There are no CloudWatch alarms.
- **MCP host.** `t3.micro` on AL2023, runs the slim `mcp-<sha>` image under systemd on loopback `:8000`, no
  ingress, reached by SSM port-forward. Read-only Postgres role, schemas `gold,silver`.
- **Bootstrap root.** OIDC provider for GitHub, role `aurum-github-actions` with `AdministratorAccess`. The trust
  policy allows exactly three subjects: `ref:refs/heads/main`, `environment:apply`, `environment:destroy`.

## 4. Variables worth knowing

| Variable | Default | Note |
|---|---|---|
| `image_tag`, `mcp_image_tag` | none | No default on purpose: the repo is immutable, so a floating tag never exists. `deploy.sh` sets both from the commit SHA |
| `db_instance_class` | `db.m7g.large` | single largest cost line; the default is what a recreate falls back to |
| `db_publicly_accessible` | `true` | with `db_ingress_cidrs` default `["0.0.0.0/0"]`; see the security review |
| `db_allocated_storage` / `db_max_allocated_storage` | 100 / 100 | |
| `monthly_budget_usd` | 150 | |
| `artifact_retention_days` | 365 | |
| `mcp_enabled` | `true` | `false` removes the EC2 host |
| `allow_destroy` | `false` | `true` drops RDS deletion protection and enables force-delete on ECR and the artifacts bucket |
| `final_snapshot_identifier` | empty | empty means destroying RDS loses the data |
| `alert_email`, `db_password`, `sec_user_agent`, `mcp_password` | secrets / no default | passed as `TF_VAR_*`, never `-var` |

## 5. Workflow

Everything goes through **`scripts/deploy.sh`**, so a local deploy and a CI deploy run the same steps. The
Makefile wraps it.

| Command | Make target | What it does |
|---|---|---|
| `deploy.sh bootstrap` | `make tf-bootstrap` | create the state bucket if missing, import an existing OIDC provider / role, apply the bootstrap root |
| `deploy.sh plan` | `make tf-plan` | `terraform plan` of the main stack, no image build |
| `deploy.sh apply` | `make tf-apply` | create ECR if absent → push the app image and `mcp-` image if their tags are missing → full apply |
| `deploy.sh destroy` | `make tf-destroy [FINAL_SNAPSHOT=name]` | apply `allow_destroy=true` to the guarded resources, then `terraform destroy` |
| (docker only) | `make build`, `login`, `push`, `print-tag` | build / push the app image by hand; `push` is the rollback path |

In CI, `terraform.yml` runs `validate` on PRs and pushes, and `apply` on merge to `main` behind a
reviewer-gated environment, authenticating with GitHub OIDC. A cold account needs one-time keys for
**Bootstrap AWS**. The full procedure, secrets, OIDC repair and destroy flow are in [`cicd.md`](cicd.md) §3.

`terraform.tfvars` (local secrets) and any `tfplan` file are gitignored.

## 6. Why the SEC egress question mattered

The original design said EDGAR could not run in the cloud because "SEC blocks cloud IPs". That was never
measured. Probed from a Fargate task in `us-east-1` on 2026-09-06 with the honest `SEC_USER_AGENT` from SSM,
`www.sec.gov/cgi-bin/browse-edgar`, `data.sec.gov/submissions/...` and `data.sec.gov/api/xbrl/...` all returned
**200**. What SEC enforces is the honest `User-Agent` and the 10 req/s cap, both of which the ingestion
framework respects, so EDGAR runs on ECS on a schedule like everything else.

## 7. Cost and security notes

- RDS (`db.m7g.large`) is the biggest line, roughly $118 a month; the budget guardrail defaults to $150.
- `db_ingress_cidrs = ["0.0.0.0/0"]` on a publicly accessible RDS is a convenience default for a single
  operator. Narrow it to your own addresses; see [`../security/risks.md`](../security/risks.md).
- The MCP HTTP transport has no authentication of its own. Its only protection is network: loopback bind, no
  security-group ingress, SSM access gated by IAM.

## 8. Not built: the original plan

The 2026-07-12 design planned Terraform modules for Snowflake (database, schemas, warehouse, roles, grants),
Kafka topics, and Postgres roles and grants, with docker-compose running the Kafka, Postgres and Airflow
containers. None of it exists: there is no Snowflake provider, no Kafka and no `infra/docker-compose.yml`. If
Kafka or Snowflake land, they would add modules to the main root in the same style; the earlier layout and
dependency diagram are in git history (this file before 2026-10-25).
