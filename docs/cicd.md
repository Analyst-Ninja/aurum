# AURUM — CI/CD Pipeline

**Version:** 1.0
**Date:** 2026-07-12
**Status:** Implemented (workflows live in `.github/workflows/`)
**Related:** [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) · [infra-as-code.md](infra-as-code.md)

---

## 1. Design

Two workflows, one principle: **CI validates everything; anything touching live local infra applies locally.**

| Workflow | File | Triggers | Jobs |
|----------|------|----------|------|
| CI | `.github/workflows/ci.yml` | push to `main`, all PRs | `python` (ruff lint + pytest) → `sonarqube` (scan + quality gate) |
| Terraform | `.github/workflows/terraform.yml` | push/PR touching `infra/terraform/**` | `validate` (fmt, validate, tflint) |

## 2. CI workflow

### `python` job
- `uv sync` against `pyproject.toml` (Python 3.12)
- **ruff** over `src/` and `main.py` — fails the build on lint errors
- **pytest** over `tests/` — auto-skips while the directory doesn't exist yet; becomes enforcing the moment tests land

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

Validation only, per the [infra-as-code.md](infra-as-code.md) decision (local state, local endpoints):

- `terraform fmt -check -recursive` — style
- `terraform init -backend=false` + `terraform validate` — syntax/provider schema without touching state
- `tflint --recursive` — provider-aware linting

**`plan` and `apply` stay local** — the kafka/postgres providers target compose endpoints on the operator machine, and tfstate is local + gitignored. CI can't and shouldn't reach either. Local flow: `terraform plan` → review → `terraform apply` (infra-as-code.md §6).

Path-filtered: the workflow only fires when `infra/terraform/**` changes, so it stays silent until the modules exist.

## 4. Branch protection (recommended setup)

On `main`: require PRs, require status checks `Lint & test`, `SonarQube analysis`, and `Format, validate, lint` (when infra changes) to pass before merge.

## 5. Future evolution

| When | Change |
|------|--------|
| Tests exist | pytest becomes hard gate (already automatic); add coverage report → `sonar.python.coverage.reportPaths` |
| dbt project lands | Add job: `dbt build` against a Snowflake CI schema, `sqlfluff` lint |
| Docker images per component | Add build+push job (GHCR), compose pulls tagged images |
| Infra grows past local | Revisit: remote state + Snowflake-module apply from CI (rejected for now — see infra-as-code.md) |

---

*Decisions (user-confirmed 2026-07-12): self-hosted SonarQube (not SonarCloud); Terraform CI = validate/lint only, apply stays local.*
