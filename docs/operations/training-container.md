# AURUM — Training Container

**Status:** Implemented ([#57](https://github.com/Analyst-Ninja/aurum/issues/57), Phase 6);
extended to all three workloads in [#72](https://github.com/Analyst-Ninja/aurum/issues/72)
**Related:** [training-and-retraining.md](../modeling/training-and-retraining.md) §9 ·
[pipeline-runbook.md](../modeling/pipeline-runbook.md) · [cicd.md](cicd.md) ·
[aws-deployment-plan.md](../infra/aws-deployment-plan.md)

---

## 1. What this is

One image and one compose service that run the modelling CLI with a pinned dependency
set. Training on the host works, but "works" depends on whatever Python happens to be
installed: `metadata.json` *records* package versions, it does not *enforce* them. The
container closes that gap — `uv sync --locked` resolves nothing, it installs `uv.lock`.

**One image, three workloads.** #57 built this for training only. #72 widened it to
ingestion and dbt as well, because the AWS deployment runs all three from ECS and one
image is simpler to build, tag and promote than three
([aws-deployment-plan.md](../infra/aws-deployment-plan.md) §4). No Airflow, no MLflow
service, no model serving — those remain out of scope.

Files:

| File | Purpose |
|------|---------|
| `docker/aurum.Dockerfile` | `python:3.12-slim` + uv + the `modeling` **and** `dbt` groups, non-root |
| `docker/entrypoint.sh` | dispatches on the first argument: `ingest`, `dbt`, `model` |
| `docker/dbt/profiles.yml` | env-var-templated dbt profile, used **only** inside the container |
| `docker-compose.modeling.yml` | the `trainer` service: env, host networking, bind mounts |
| `.dockerignore` | keeps the build context to source + lockfile |

## 2. Running it

```bash
docker compose -f docker-compose.modeling.yml build trainer     # first time, or after uv.lock moves

docker compose -f docker-compose.modeling.yml run --rm trainer \
  model train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

**The first argument after `trainer` picks the workload**; everything after it goes to that
CLI unchanged, so the container takes the same arguments the host CLIs do:

```bash
docker compose -f docker-compose.modeling.yml run --rm trainer model  train    -c <cfg>
docker compose -f docker-compose.modeling.yml run --rm trainer model  backtest -c <cfg> --version latest
docker compose -f docker-compose.modeling.yml run --rm trainer ingest -c src/ingestion/configs/yahoo/ohlcv_1d.yaml -f False
docker compose -f docker-compose.modeling.yml run --rm trainer dbt    build --select bronze
docker compose -f docker-compose.modeling.yml run --rm trainer dbt    debug
```

Anything that is not `ingest`, `dbt` or `model` is `exec`'d verbatim, so `sh`, `id` and
`--help` still work for debugging.

> **Migrating from the #57 form:** the entrypoint used to be `python -m src.modeling.cli`,
> so `run --rm trainer train -c …` worked directly. It is now
> `run --rm trainer **model** train -c …`.

The full 11-step pipeline, including the dbt steps that run on the host, is in
[pipeline-runbook.md](../modeling/pipeline-runbook.md).

## 3. Five decisions worth knowing

### Postgres stays on the host

The container reaches the host's database at `host.docker.internal`; compose overrides the
`HOST` variable from `.env` to that name and adds `host.docker.internal:host-gateway` so it
resolves on Linux as well as Docker Desktop.

There is no `postgres` service in the compose file on purpose. The training panel is ~2.9M
rows built by the whole dbt medallion; standing up a database inside compose would mean
loading all of it in before a single model could train. For a local-first project, reaching
the host is the honest simplification.

Nothing in `src/modeling/configs/*.yaml` changes for the container — the configs reference
env var *names* (`host: HOST`), and only the value differs.

### Credentials come from `.env`, never the image

`env_file: .env` passes `HOST`, `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD` in as environment
variables at run time. `.env` is in `.dockerignore`, so it is not in the build context and
cannot end up in a layer.

### The commit has to be passed in

`models/<version>/` is named `{date}-{git short sha}`, and the image contains neither the
`.git` directory nor a git binary. Left alone, every container run would be stamped
`{date}-unknown`, and two runs on the same day would silently overwrite each other.

So `git_sha()` honours `AURUM_GIT_SHA`, and compose forwards it:

```bash
AURUM_GIT_SHA=$(git rev-parse HEAD) \
  docker compose -f docker-compose.modeling.yml run --rm trainer \
    model train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

Unset, it falls back to `git rev-parse` (which is how host runs resolve it) and then to
`unknown`. Always set it for a run whose artifacts you intend to keep.

### dbt needs its own sync layer, and it cannot use `--no-build`

The Dockerfile runs `uv sync` **twice**. The first is the ~400 MB ML stack under
`--no-build`, mirroring CI: every modelling dependency ships a wheel, and that layer only
rebuilds when `uv.lock` moves. The second adds the `dbt` group **without** `--no-build`,
because `dbt-core` pulls `dbt-core-experimental-parser`, which publishes an sdist and no
wheel.

That is the same constraint that keeps `dbt-postgres` out of `[project].dependencies` in the
first place (see the comment in `pyproject.toml`). CI's install path is unchanged and still
runs `--no-build`; only the image relaxes it, and only for that one group.

`dbt_packages/` is gitignored, so it is not in the build context either — the image runs
`dbt deps` at build time to vendor dbt_utils, dbt_expectations and dbt_date. A container
therefore needs no network access to run `dbt build`.

### The container's dbt profile is not the host's

`docker/dbt/profiles.yml` is templated on the same env vars everything else reads
(`HOST`, `PORT`, `AURUM_USERNAME`, `AURUM_PASSWORD`), and `DBT_PROFILES_DIR` points at it.

It deliberately lives under `docker/` rather than inside `src/transformation/aurum_dwh/`.
dbt checks the working directory before `~/.dbt`, so a committed `profiles.yml` in the
project directory would silently hijack **every host run** — `uv run --group dbt dbt debug`
on your machine must keep resolving `~/.dbt/profiles.yml`, and it does.

It also sets `threads: 4`, matching the host profile. This was `2`, sized for the 1 GB
`db.t4g.micro` the deployment originally targeted, where four concurrent
window-function-heavy models exhausted the instance. RDS is `db.m7g.large` now — 2 vCPU,
8 GB, non-burstable — and two threads left it idle for most of the build
([aws-deployment-plan.md](../infra/aws-deployment-plan.md) §5). Raise it only alongside
the instance class: threads beyond the database's vCPU count add contention, not
throughput.

### Artifacts live on the host

Two bind mounts:

- `./models` → `/app/models` — the run registry. Without it, a trained model dies with the
  container.
- `./data` → `/app/data` — the Parquet training cache. Shared with host runs, so a container
  run reuses a cache the host built and vice versa (the cache is keyed on the source table's
  `max(date)`).

## 4. Non-root

The image creates and runs as `aurum` (uid 1000). Verify with:

```bash
docker compose -f docker-compose.modeling.yml run --rm --entrypoint id trainer
# uid=1000(aurum) gid=1000(aurum) groups=1000(aurum)
```

uid 1000 is the first regular user on Linux, which is what makes files written into the
`models/` mount owned by the host user rather than by root. If your host uid differs, add
`user: "${UID}:${GID}"` to the service rather than reverting to root.

## 5. Memory

The container needs more RAM than a default Docker Desktop VM gives it. The panel is
2.9M rows x 228 float32 columns — ~2.6 GB before preprocessing makes its copies — and an
8 GB VM gets the training process OOM-killed shortly after the load, which surfaces as a
bare `exit code 137` with no Python traceback.

**Give the Docker VM at least 12 GB** (Docker Desktop → Settings → Resources → Memory).
Host runs do not hit this because they use the whole machine's memory.

A cheap way to check the plumbing without the full schedule — a two-fold window trains in
a couple of minutes and fits in 8 GB:

```bash
cp src/modeling/configs/lgbm_xs_excess_5d.yaml /tmp/smoke.yaml
cat >> /tmp/smoke.yaml <<'YAML'
splits:
  burn_in_folds: 290
  eval_end_fold: 292
YAML
AURUM_GIT_SHA=$(git rev-parse HEAD) \
  docker compose -f docker-compose.modeling.yml run --rm \
    -v /tmp/smoke.yaml:/app/smoke.yaml trainer model train -c smoke.yaml
```

## 6. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `could not translate host name "host.docker.internal"` | Older Docker on Linux. The `extra_hosts` mapping handles it; if the daemon is too old to support `host-gateway`, use `network_mode: host` and set `HOST: localhost`. |
| `connection refused` on port 5432 | Host Postgres is bound to loopback only. Set `listen_addresses = '*'` in `postgresql.conf` and allow the Docker bridge subnet (typically `172.17.0.0/16`) in `pg_hba.conf`. |
| `libgomp.so.1: cannot open shared object file` | The image is missing `libgomp1`, which LightGBM's wheel links against. It is installed in the Dockerfile — this means the layer was skipped, so rebuild without cache. |
| `The lockfile is not up-to-date` during build | `pyproject.toml` changed without `uv lock`. Run `uv lock` on the host and rebuild; CI fails on the same drift. |
| Artifacts missing from `models/` after a run | Started with `docker run` instead of compose, so the bind mounts were absent. |
| `exit code 137`, no traceback | The kernel OOM-killed the process. See §5 — raise the Docker VM to 12 GB. |
| `unrecognized arguments: train` or the modelling `--help` when you wanted a run | The #57 entrypoint went straight to the modelling CLI; it now dispatches on the first argument. Use `trainer model train …`, not `trainer train …`. |
| `Could not find command, ensure it is in the user's PATH: "git"` from `dbt debug` | The image is missing `git`, which `dbt debug` probes for. It is installed in the Dockerfile — the check is cosmetic (packages are vendored at build time), but it makes `dbt debug` exit non-zero, which would fail a Step Functions `runTask.sync` state. Rebuild without cache. |
| `Runtime Error … profiles.yml` inside the container | `DBT_PROFILES_DIR` is unset or `docker/` was not copied into the image. Both are set in the Dockerfile. |

## 7. Verified run

`20260906-9a3117a` under `models/` was trained entirely in the container against the host's
Postgres: 2,896,633 rows loaded, 15 refits, mean validation IC 0.0325, ~9.5 minutes with a
warm Parquet cache and a peak of ~8.5 GB. The artifacts are owned by the host user, and the
version id carries the real commit because `AURUM_GIT_SHA` was passed in.

## 8. Not included

No image publishing (no GHCR push job), no Airflow DAG, no serving container, no Terraform
wiring. `terraform.yml` is path-filtered on `infra/terraform/**`, which does not exist yet —
that is deliberate ([infra-as-code.md](infra-as-code.md) §5) and this image does not change it.
