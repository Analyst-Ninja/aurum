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
| Terraform | `.github/workflows/terraform.yml` | PR touching `infra/terraform/**`; push to `main` touching that or anything in the image (`src/`, `docker/`, `main.py`, `pyproject.toml`, `uv.lock`) | `validate` (fmt, validate, tflint) → `apply` (build + push the image, then `terraform apply`) |

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

Two jobs. `validate` is path-filtered to `infra/terraform/**`; the push trigger also
covers everything the container image contains — `src/**`, `docker/**`, `main.py`,
`pyproject.toml`, `uv.lock` — because this workflow is what ships application code to
Fargate as well as infrastructure. A docs-only change still fires nothing.

**`validate`** — runs on pushes to `main` and on PRs:

- `terraform fmt -check -recursive` — style
- `terraform init -backend=false` + `terraform validate` — syntax/provider schema without touching state
- `tflint --recursive` — provider-aware linting

**`apply`** — `needs: validate`, and only on a push to `main`, or a manual `workflow_dispatch` on `main` (GH-84). Merging an infra change applies it; nothing applies from a PR or any other branch, and there is no approval gate. The manual trigger exists because the path filter means a workflow-only or docs-only change never fires a run — without it there is no way to apply after fixing the job itself, and re-running an old run replays that commit's workflow file. The `github.ref` guard is belt-and-braces: a dispatch from another branch could not assume the role anyway, since the trust policy pins `ref:refs/heads/main`.

The original decision was "apply stays local", on two grounds that no longer hold: the kafka/postgres providers targeting compose endpoints (the only provider left is `hashicorp/aws`) and local gitignored state (`versions.tf` moved to an S3 backend with `use_lockfile = true`). What replaced them:

| Concern | How the job handles it |
|---|---|
| AWS credentials | GitHub OIDC. `aws-actions/configure-aws-credentials` assumes `aurum-github-actions` (`infra/terraform/github_oidc.tf`); the trust policy is `StringEquals` on `sub = repo:Analyst-Ninja/aurum:ref:refs/heads/main`, so no other repo, branch or fork PR can assume it. No long-lived keys exist anywhere. |
| State locking | `concurrency: { group: terraform-apply, cancel-in-progress: false }`. The S3 backend's native lock fails a concurrent run outright, and cancelling mid-apply strands the lock. |
| `image_tag` | The short SHA of the last commit that touched anything the image contains (`git log -1 -- pyproject.toml uv.lock src docker main.py`), **not** of HEAD. The job then builds and pushes that tag if it is not already in ECR, so the tag always names an image that exists. See "Shipping code" below. |
| Secrets | `TF_VAR_DB_PASSWORD`, `TF_VAR_SEC_USER_AGENT` and `TF_VAR_ALERT_EMAIL` as repository secrets, injected as env — never as `-var` on the command line. A preflight step fails the job if any is empty: an unset secret renders as `""`, and `TF_VAR_x=""` counts as *set* to Terraform, so it overrides the variable's default instead of falling through to it. The first run learned this the hard way — an empty `alert_email` destroyed the SNS email subscription and then failed to recreate it (`InvalidParameter: Endpoint`), leaving alerts silent until the values were set. |

**Bootstrap.** The role has to exist before a run can assume it, so the first apply after `github_oidc.tf` landed was a local `terraform apply`. If the account already has an OIDC provider for `token.actions.githubusercontent.com`, import it — an account holds only one per URL.

**Shipping code.** The `apply` job builds and pushes the image itself, so a merge to
`main` that changes `src/` reaches Fargate without a local step:

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
