# AURUM — Training Container

**Status:** Implemented ([#57](https://github.com/Analyst-Ninja/aurum/issues/57), Phase 6)
**Related:** [training-and-retraining.md](../modeling/training-and-retraining.md) §9 ·
[pipeline-runbook.md](../modeling/pipeline-runbook.md) · [cicd.md](cicd.md)

---

## 1. What this is

One image and one compose service that run the modelling CLI with a pinned dependency
set. Training on the host works, but "works" depends on whatever Python happens to be
installed: `metadata.json` *records* package versions, it does not *enforce* them. The
container closes that gap — `uv sync --locked` resolves nothing, it installs `uv.lock`.

**Scope is training only.** No Airflow, no MLflow service, no model serving — those are
Phases 7 and 8 and are deliberately out of scope here.

Files:

| File | Purpose |
|------|---------|
| `docker/modeling.Dockerfile` | `python:3.12-slim` + uv + the `modeling` group, non-root |
| `docker-compose.modeling.yml` | the `trainer` service: env, host networking, bind mounts |
| `.dockerignore` | keeps the build context to source + lockfile |

## 2. Running it

```bash
docker compose -f docker-compose.modeling.yml build trainer     # first time, or after uv.lock moves

docker compose -f docker-compose.modeling.yml run --rm trainer \
  train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

The entrypoint is `python -m src.modeling.cli`, so **everything after `trainer` is a CLI
subcommand** — the container takes the same arguments the host CLI does:

```bash
docker compose -f docker-compose.modeling.yml run --rm trainer --help
docker compose -f docker-compose.modeling.yml run --rm trainer evaluate        -c <cfg> --version latest
docker compose -f docker-compose.modeling.yml run --rm trainer select-features -c <cfg> --version latest
docker compose -f docker-compose.modeling.yml run --rm trainer backtest        -c <cfg> --version latest
```

The full 11-step pipeline, including the dbt steps that run on the host, is in
[pipeline-runbook.md](../modeling/pipeline-runbook.md).

## 3. Three decisions worth knowing

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
    train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
```

Unset, it falls back to `git rev-parse` (which is how host runs resolve it) and then to
`unknown`. Always set it for a run whose artifacts you intend to keep.

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

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `could not translate host name "host.docker.internal"` | Older Docker on Linux. The `extra_hosts` mapping handles it; if the daemon is too old to support `host-gateway`, use `network_mode: host` and set `HOST: localhost`. |
| `connection refused` on port 5432 | Host Postgres is bound to loopback only. Set `listen_addresses = '*'` in `postgresql.conf` and allow the Docker bridge subnet (typically `172.17.0.0/16`) in `pg_hba.conf`. |
| `libgomp.so.1: cannot open shared object file` | The image is missing `libgomp1`, which LightGBM's wheel links against. It is installed in the Dockerfile — this means the layer was skipped, so rebuild without cache. |
| `The lockfile is not up-to-date` during build | `pyproject.toml` changed without `uv lock`. Run `uv lock` on the host and rebuild; CI fails on the same drift. |
| Artifacts missing from `models/` after a run | Started with `docker run` instead of compose, so the bind mounts were absent. |

## 6. Not included

No image publishing (no GHCR push job), no Airflow DAG, no serving container, no Terraform
wiring. `terraform.yml` is path-filtered on `infra/terraform/**`, which does not exist yet —
that is deliberate ([infra-as-code.md](infra-as-code.md) §5) and this image does not change it.
