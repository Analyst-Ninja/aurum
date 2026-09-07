# AURUM — CI/CD Pipeline

**Version:** 1.0
**Date:** 2026-07-12
**Status:** Implemented (workflows live in `.github/workflows/`)
**Related:** [TECHNICAL_SPEC.md](../architecture/TECHNICAL_SPEC.md) · [infra-as-code.md](infra-as-code.md)

---

## 1. Design

Two workflows, one principle: **CI validates everything; anything touching live local infra applies locally.**

| Workflow | File | Triggers | Jobs |
|----------|------|----------|------|
| CI | `.github/workflows/ci.yml` | push to `main`, all PRs | `python` (ruff lint + pytest) → `sonarqube` (scan + quality gate) |
| Terraform | `.github/workflows/terraform.yml` | push/PR touching `infra/terraform/**` | `validate` (fmt, validate, tflint) |

## 2. CI workflow

### `python` job
- `uv sync --locked --no-build --group modeling` (Python 3.12) — `--locked` fails the build on
  `pyproject.toml` / `uv.lock` drift
- **ruff** over `src/` and `main.py` — fails the build on lint errors
- **pytest** over `tests/` — a hard gate since [#57](https://github.com/Analyst-Ninja/aurum/issues/57);
  91 tests cover `src/modeling/`. The install step is `uv sync --locked --no-build --group modeling`,
  because those tests import `lightgbm`, `pyarrow` and `scikit-learn`. `dbt` keeps its own group and
  stays out — `dbt-core` pulls an sdist-only dependency that `--no-build` cannot install, while every
  modeling dependency ships manylinux wheels

### `sonarqube` job (self-hosted SonarQube)
- Runs after `python` passes; skipped on fork PRs (no secrets there)
- `sonarsource/sonarqube-scan-action` with `fetch-depth: 0` (full history for new-code detection)
- `sonarqube-quality-gate-action` **fails the build if the quality gate fails** — this is the enforcement point
- Scanner config in `sonar-project.properties` (sources = `src/`, `main.py`; notebooks/docs/infra excluded)

**Required GitHub secrets:**

| Secret | Value |
|--------|-------|
| `SONAR_HOST_URL` | URL of your SonarQube server |
| `SONAR_TOKEN` | Project analysis token generated in SonarQube |

⚠️ **Reachability:** GitHub-hosted runners must reach `SONAR_HOST_URL`. A SonarQube on `localhost` won't work from CI. Options, in order of preference:
1. SonarQube on a small VPS / always-on box with a public URL (basic auth + HTTPS)
2. Tunnel to your local server (e.g., Cloudflare Tunnel) exposing a stable hostname
3. Fallback: run the scanner locally (`sonar-scanner` CLI) and keep CI's quality signal to ruff/pytest only

Server bootstrap (local): `docker run -d --name sonarqube -p 9000:9000 sonarqube:community` → create project `aurum` → generate token.

## 3. Terraform workflow

Two jobs, both path-filtered to `infra/terraform/**` so the workflow stays silent on every other change.

**`validate`** — runs on pushes to `main` and on PRs:

- `terraform fmt -check -recursive` — style
- `terraform init -backend=false` + `terraform validate` — syntax/provider schema without touching state
- `tflint --recursive` — provider-aware linting

**`apply`** — `needs: validate`, and only on a push to `main` (GH-84). Merging an infra change applies it; nothing applies from a PR or any other branch, and there is no approval gate.

The original decision was "apply stays local", on two grounds that no longer hold: the kafka/postgres providers targeting compose endpoints (the only provider left is `hashicorp/aws`) and local gitignored state (`versions.tf` moved to an S3 backend with `use_lockfile = true`). What replaced them:

| Concern | How the job handles it |
|---|---|
| AWS credentials | GitHub OIDC. `aws-actions/configure-aws-credentials` assumes `aurum-github-actions` (`infra/terraform/github_oidc.tf`); the trust policy is `StringEquals` on `sub = repo:Analyst-Ninja/aurum:ref:refs/heads/main`, so no other repo, branch or fork PR can assume it. No long-lived keys exist anywhere. |
| State locking | `concurrency: { group: terraform-apply, cancel-in-progress: false }`. The S3 backend's native lock fails a concurrent run outright, and cancelling mid-apply strands the lock. |
| `image_tag` | Read off the live `aurum-ingest-market` task definition, not from the commit SHA. Nothing in CI builds or pushes an image — `make push` is still local — so passing this commit's SHA would repoint all four task definitions at an image that was never pushed. The step fails loudly rather than defaulting. |
| Secrets | `TF_VAR_DB_PASSWORD`, `TF_VAR_SEC_USER_AGENT` and `TF_VAR_ALERT_EMAIL` as repository secrets, injected as env — never as `-var` on the command line. A preflight step fails the job if any is empty: an unset secret renders as `""`, and `TF_VAR_x=""` counts as *set* to Terraform, so it overrides the variable's default instead of falling through to it. The first run learned this the hard way — an empty `alert_email` destroyed the SNS email subscription and then failed to recreate it (`InvalidParameter: Endpoint`), leaving alerts silent until the values were set. |

**Bootstrap.** The role has to exist before a run can assume it, so the first apply after `github_oidc.tf` landed was a local `terraform apply`. If the account already has an OIDC provider for `token.actions.githubusercontent.com`, import it — an account holds only one per URL.

**Still local:** building and pushing the container image (`make push`), and any `terraform plan` you want to eyeball before merging.

## 4. Branch protection (recommended setup)

On `main`: require PRs, require status checks `Lint & test`, `SonarQube analysis`, and `Format, validate, lint` (when infra changes) to pass before merge.

## 5. Future evolution

| When | Change |
|------|--------|
| Coverage wanted | add `pytest --cov` and a coverage report → `sonar.python.coverage.reportPaths` |
| dbt project lands | Add job: `dbt build` against a Snowflake CI schema, `sqlfluff` lint |
| Docker images per component | Add build+push job (GHCR), compose pulls tagged images. The training
image ([training-container.md](training-container.md)) is built locally for now — CI does not build or push it |
| ~~Infra grows past local~~ | ~~Revisit: remote state + apply from CI~~ — **done** (GH-84): state is S3, apply runs on merge to `main` via OIDC. See §3. |

---

*Decisions (user-confirmed 2026-07-12): self-hosted SonarQube (not SonarCloud); Terraform CI = validate/lint only, apply stays local.*
*Amended 2026-09-07 (GH-84): apply now runs in CI on merge to `main`. Both premises behind "apply stays local" had expired — see §3.*
