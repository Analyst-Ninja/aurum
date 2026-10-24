# AURUM — CI/CD Pipeline

**Version:** 2.0
**Updated:** 2026-10-25
**Status:** Implemented (workflows live in `.github/workflows/`)
**Related:** [TECHNICAL_SPEC.md](../architecture/TECHNICAL_SPEC.md) · [infra-as-code.md](infra-as-code.md)

---

## 1. Design

Four workflows, one principle: **CI validates everything; merging to `main` ships it.**

| Workflow | File | Triggers | What it does |
|----------|------|----------|------|
| CI | `ci.yml` | push to `main`, `develop`, `epic/*`; PRs (opened, synchronize, reopened) | one job, `Lint, test & SonarQube`: install, ruff, pytest with coverage, Sonar scan |
| Terraform | `terraform.yml` | PR touching `infra/terraform/**` or `scripts/deploy.sh`; push to `main` touching those or anything in the image (`src/`, `docker/`, `main.py`, `pyproject.toml`, `uv.lock`); manual `workflow_dispatch` | `validate` (fmt, validate, tflint) → `apply` (build + push both images, then `terraform apply`), gated by the `apply` environment |
| Bootstrap AWS | `bootstrap.yml` | manual, `main` only | creates the state bucket and the GitHub OIDC role, or repairs the role's trust policy |
| Terraform destroy | `terraform-destroy.yml` | manual; type `destroy-aurum` to confirm | tears the stack down in the `destroy` environment, with an optional final RDS snapshot |

## 2. CI workflow

One job, `Lint, test & SonarQube`, on `ubuntu-latest`. Actions are pinned by full commit SHA; the
`GITHUB_TOKEN` is read-only.

1. **Install** — `uv sync --locked --no-build --group modeling --group mcp --group paper --group backtesting`
   (Python 3.12). `--locked` fails the build on `pyproject.toml` / `uv.lock` drift. The extra groups are
   there because the tests import `lightgbm`, `pyarrow` and `scikit-learn` (modeling), the `mcp` SDK, `alpaca-py`
   (paper) and backtrader / vectorbt (backtesting). `dbt` stays out: `dbt-core` pulls an sdist-only dependency
   that `--no-build` cannot install, while every other dependency ships manylinux wheels. Lumibot is not
   installed; its engine runs in a child process and its tests do not need the package.
2. **Lint** — `ruff check src/ main.py`. A lint error fails the build.
3. **Tests** — `pytest tests/ -v --cov=src --cov-report=xml`. A hard gate since
   [#57](https://github.com/Analyst-Ninja/aurum/issues/57). Covers `src/ingestion`, `src/modeling`,
   `src/inference`, `src/mcp`, `src/paper` and `src/utils`. dbt's own tests are not run in CI; they run in the
   `aurum-dbt` task after every ingest.
4. **Sonar scan** — `sonarqube-scan-action` against **SonarCloud** (org `analyst-ninja`, project
   `Analyst-Ninja_aurum`, set in `sonar-project.properties`). Coverage must come from the same job, because the
   scanner reads `coverage.xml` off the runner's filesystem. The quality gate is enforced as the *SonarCloud Code
   Analysis* check on the PR (new-code coverage threshold 80%).

Scanner scope: sources `src/` and `main.py`, tests in `tests/`, inclusions `src/**`; `docs/**`, `nbs/**` and the
dbt SQL are excluded.

**Required GitHub secret:** `SONAR_TOKEN` (project analysis token). The scan is skipped on fork PRs, which have
no secrets.

## 3. Terraform workflow

Two jobs. `validate` is path-filtered to `infra/terraform/**`; the push trigger also
covers everything the container image contains — `src/**`, `docker/**`, `main.py`,
`pyproject.toml`, `uv.lock` — because this workflow is what ships application code to
Fargate as well as infrastructure. A docs-only change still fires nothing.

**`validate`** — runs on pushes to `main` and on PRs:

- `terraform fmt -check -recursive` — style
- `terraform init -backend=false` + `terraform validate` — syntax/provider schema without touching state
- `tflint --recursive` — provider-aware linting

**`apply`** — `needs: validate`, and only on a push to `main`, or a manual `workflow_dispatch` on `main` (GH-84). Merging an infra change queues an apply behind the `apply` environment's required reviewer gate; nothing applies from a PR or any other branch. The manual trigger exists because the path filter means a workflow-only or docs-only change never fires a run — without it there is no way to apply after fixing the job itself, and re-running an old run replays that commit's workflow file. The `github.ref` guard is belt-and-braces: a dispatch from another branch cannot reach deployment because the environment is restricted to `main`.

The original decision was "apply stays local", on two grounds that no longer hold: the kafka/postgres providers targeting compose endpoints (the only provider left is `hashicorp/aws`) and local gitignored state (`versions.tf` moved to an S3 backend with `use_lockfile = true`). What replaced them:

| Concern | How the job handles it |
|---|---|
| AWS credentials | GitHub OIDC. `aws-actions/configure-aws-credentials` assumes `aurum-github-actions` (`infra/terraform/bootstrap/github_oidc.tf`); the trust policy is `StringEquals` on the main-branch subject and the reviewer-gated `apply`/`destroy` environment subjects, so no other repo, branch or fork PR can assume it. No long-lived keys exist anywhere. |
| State locking | `concurrency: { group: terraform-apply, cancel-in-progress: false }`. The S3 backend's native lock fails a concurrent run outright, and cancelling mid-apply strands the lock. |
| `image_tag` | The short SHA of the last commit that touched anything the image contains (`git log -1 -- pyproject.toml uv.lock src docker main.py`), **not** of HEAD. The job then builds and pushes that tag if it is not already in ECR, so the tag always names an image that exists. See "Shipping code" below. |
| Secrets | `TF_VAR_DB_PASSWORD`, `TF_VAR_SEC_USER_AGENT` and `TF_VAR_ALERT_EMAIL` as repository secrets, injected as env — never as `-var` on the command line. A preflight step fails the job if any is empty: an unset secret renders as `""`, and `TF_VAR_x=""` counts as *set* to Terraform, so it overrides the variable's default instead of falling through to it. The first run learned this the hard way — an empty `alert_email` destroyed the SNS email subscription and then failed to recreate it (`InvalidParameter: Endpoint`), leaving alerts silent until the values were set. |

**First-time setup (no local deploy).** A cold account has nothing for GitHub to trust, so one step needs real credentials. The OIDC provider and role live in their own root, `infra/terraform/bootstrap/` (state key `aurum/bootstrap.tfstate`), so destroying the main stack can never delete the role the destroy run is using.

1. Create an IAM user with `AdministratorAccess`; add its keys as repository secrets `AWS_BOOTSTRAP_ACCESS_KEY_ID` and `AWS_BOOTSTRAP_SECRET_ACCESS_KEY`. Also set `TF_VAR_DB_PASSWORD`, `TF_VAR_SEC_USER_AGENT`, `TF_VAR_ALERT_EMAIL`, `TF_VAR_MCP_PASSWORD`.
2. Run **Bootstrap AWS** (Actions tab, `workflow_dispatch` on `main`) with `auth_method = bootstrap-keys`. It runs `scripts/deploy.sh bootstrap`: creates the versioned state bucket if missing, imports an existing OIDC provider if the account has one, applies the bootstrap root.
3. Delete the two bootstrap secrets and the IAM user. Everything from here uses OIDC.
4. Push to `main` (or run **Terraform** manually). `scripts/deploy.sh apply` creates the ECR repo first, pushes the main and `mcp-` images, then does the full apply.
5. **One manual step stays: create the MCP read-only Postgres role, once, as the RDS admin.** Terraform and CI cannot do it: the AWS provider has no Postgres-role resource, GitHub runners cannot reach RDS (the security group admits only listed CIDRs and the MCP host), and the role needs the `gold` and `silver` schemas that only exist after the first `dbt build`. Terraform only stores the password in SSM; it never creates the role.

**OIDC trust repair.** If a later change edits `infra/terraform/bootstrap/github_oidc.tf`, run **Bootstrap AWS** again with the default `auth_method = oidc`. The job has no GitHub environment, so its token keeps the `ref:refs/heads/main` subject and can use the existing role to update the reviewer-gated `apply`/`destroy` subjects.

   Placeholders: `<ADMIN_USER>` / `<ADMIN_PASS>` = the RDS master login (`TF_VAR_DB_USER` / `TF_VAR_DB_PASSWORD`); `<OWNER_ROLE>` = the role dbt runs as and owns the tables (`aurum`); `<MCP_LOGIN_PASS>` = the new role's password, **identical** to `TF_VAR_MCP_PASSWORD` / `mcp_password` in `terraform.tfvars`; `<RDS_HOST>` = the RDS endpoint.

   1. Wait for the first dbt run to build `silver` and `gold` (the daily `aurum-daily-market` machine, or start it by hand).
   2. Connect to database `aurum` as `<ADMIN_USER>` from a host the security group allows (your laptop through the existing DB access path, or a DB client):

      ```bash
      psql "host=<RDS_HOST> port=5432 dbname=aurum user=<ADMIN_USER> password=<ADMIN_PASS> sslmode=require" \
        -v pw="'<MCP_LOGIN_PASS>'" -v owner=<OWNER_ROLE> \
        -f infra/sql/mcp_readonly_role.sql
      ```

      Or paste the file into any SQL client, replacing `:pw` with `'<MCP_LOGIN_PASS>'` and `:owner` with `<OWNER_ROLE>`.
   3. Check: `select rolname, rolcanlogin from pg_roles where rolname = 'aurum_mcp_ro';` returns one row with `true`.
   4. Open the tunnel and call `list_tables`. `password authentication failed for user "aurum_mcp_ro"` means the role password differs from SSM; fix with `ALTER ROLE aurum_mcp_ro PASSWORD '<MCP_LOGIN_PASS>';` as admin, no restart needed. Full detail: `docs/mcp/mcp-server-design.md` (Setup guide).

   Redo this step after every `terraform destroy`: the role dies with RDS.

**Local deploy.** Same script, your own AWS credentials, secrets from the gitignored `infra/terraform/terraform.tfvars`: `make tf-bootstrap`, `make tf-plan`, `make tf-apply`.

**Destroy.** **Terraform destroy** is `workflow_dispatch` only, runs in the `destroy` environment (add required reviewers there) and needs `confirm` = `destroy-aurum`. `scripts/deploy.sh destroy` sets `allow_destroy=true`, which turns off RDS deletion protection and sets `force_delete` / `force_destroy` on ECR and the artifacts bucket; it applies those to the three resources, then runs `terraform destroy`. RDS data is lost unless `final_snapshot` names a snapshot. The state bucket and the bootstrap role survive, so a later push to `main` rebuilds everything. The MCP database role dies with RDS. Locally: `make tf-destroy FINAL_SNAPSHOT=aurum-final`.

**Shipping code.** The `apply` job runs `scripts/deploy.sh apply`, which builds and pushes **two**
images from `docker/aurum.Dockerfile` (the full image, tagged `<sha>`, and the slim MCP image built with
`--target mcp`, tagged `mcp-<sha>`), so a merge to `main` that changes `src/` reaches Fargate and the MCP host
without a local step. The script also creates the ECR repo first if it is missing and fails in CI if any of the
four `TF_VAR_*` secrets (`db_password`, `sec_user_agent`, `alert_email`, `mcp_password`) is empty:

1. **Resolve the tag** — the short SHA of the last commit that touched `pyproject.toml`,
   `uv.lock`, `src/`, `docker/` or `main.py`. Not HEAD: an infra-only commit must resolve
   to the tag already deployed, or every infra change would pointlessly rebuild and
   roll all four task definitions. Building the working tree under an older commit's tag
   is exact — by construction none of the COPYed paths changed in between.
2. **Build and push, if needed** — `aws ecr describe-images` first. `aws_ecr_repository.aurum`
   is `IMMUTABLE`, so re-pushing an existing tag fails; the check also makes a re-run
   idempotent, since the same commit resolves to the same tag and skips straight to the
   apply. `--platform linux/amd64`, identical to the Makefile — Fargate refuses arm64.
3. **Apply** — `TF_VAR_image_tag` is that tag, which repoints all four task definitions
   and stamps `AURUM_GIT_SHA`.

The build lives inside the `apply` job rather than a job of its own so it falls under the
same `terraform-apply` concurrency group: two pushes in quick succession cannot race a
build against an apply, and a failed build stops the job before `apply` runs.

This replaced reading `image_tag` off the live `aurum-ingest-market` task definition. That
read was a fixed point — the resolved value always equalled the deployed value, so `apply`
reported `No changes` on every run and application code could never ship from CI.

`make push` still works and is still the way to push from a laptop; CI no longer depends
on it.

**Still local:** any `terraform plan` you want to eyeball before merging, and a rollback
to an older tag (`terraform apply -var="image_tag=<older sha>"` — the workflow always
resolves forward).

## 4. Branch protection (recommended setup)

On `main`: require PRs, and require the checks `Lint, test & SonarQube`, `SonarCloud Code Analysis` and
`Format, validate, lint` (when infra changes) to pass before merge.

## 5. Future evolution

| When | Change |
|------|--------|
| dbt in CI | Run `dbt build` against a throwaway Postgres schema, plus `sqlfluff` lint. Today dbt's tests run only on AWS after ingest |
| Image checks | The images are built in the `apply` job, only on merge to `main`. A PR build with `docker build` would catch Dockerfile breakage before merge |
| ~~Infra grows past local~~ | ~~Revisit: remote state + apply from CI~~ — **done** (GH-84): state is S3, apply runs on merge to `main` via OIDC. See §3. |

---

*History: the 2026-07-12 version chose a self-hosted SonarQube and "Terraform CI = validate/lint only, apply stays local". Both changed: Sonar runs on SonarCloud, and apply moved to CI on merge to `main` (GH-84, 2026-09-07) — see §3. Updated again 2026-10-25 for the four-workflow layout and the extra dependency groups.*
