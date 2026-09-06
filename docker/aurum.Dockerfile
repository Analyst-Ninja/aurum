# One image for all three AURUM workloads: ingestion, dbt and modelling.
#
# The point is a reproducible environment: `uv sync --locked` resolves nothing, it
# installs uv.lock, so a run inside the container is the same run tomorrow. On AWS it is
# also the *only* artifact — four ECS task definitions run this image and differ only in
# the command they pass.
#
# Build:  docker compose -f docker-compose.modeling.yml build trainer
# Run:    docker compose -f docker-compose.modeling.yml run --rm trainer \
#           model train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
#
# The first argument selects the workload (`ingest` | `dbt` | `model`) — see
# docker/entrypoint.sh. Docs: docs/operations/training-container.md,
# docs/infra/aws-deployment-plan.md.

FROM python:3.12-slim

# uv comes from its own published image rather than a curl | sh, and is pinned.
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    DBT_PROFILES_DIR=/app/docker/dbt

WORKDIR /app

# libgomp1: LightGBM's wheel links against OpenMP, which python:3.12-slim does not ship —
#   without it `import lightgbm` dies on libgomp.so.1.
# git: `dbt debug` probes for a git binary and reports a failed check without one, exiting
#   non-zero. That check is cosmetic here (packages are vendored at build time), but a
#   non-zero exit fails a Step Functions runTask.sync state, and `dbt debug` is the cheapest
#   connectivity smoke test we have.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

# Two sync layers, deliberately.
#
# The first is the ~400 MB ML stack under --no-build, mirroring CI: every modeling
# dependency ships a wheel, and this layer only rebuilds when uv.lock moves.
RUN uv sync --locked --no-build --group modeling --no-install-project

# The second adds dbt, and it CANNOT use --no-build: dbt-core pulls
# dbt-core-experimental-parser, which publishes an sdist with no wheel. That is the whole
# reason the dbt group is isolated from [project].dependencies (see pyproject.toml) — CI's
# install path keeps --no-build; only the image relaxes it, and only for this group.
RUN uv sync --locked --group dbt --group modeling --no-install-project

# Then the source, which changes on every commit. src/transformation/ is included: the
# image runs dbt, and the model registry reads the dbt manifest for its lineage hash.
COPY src/ ./src/
COPY docker/ ./docker/
COPY main.py ./

# dbt_packages/ is gitignored, so it is not in the build context. Vendor the packages at
# build time or `dbt build` fails at runtime on a missing dbt_utils.
RUN cd /app/src/transformation/aurum_dwh && dbt deps

# Non-root. On AWS the EFS access point is configured with the same uid/gid so the
# bind-mounted models/ and data/ are writable; locally they are host-owned, so override
# with `user:` in compose if your uid is not 1000
# (docs/operations/training-container.md).
RUN useradd --create-home --uid 1000 aurum \
    && chmod +x /app/docker/entrypoint.sh \
    && chown -R aurum:aurum /app
USER aurum

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["model", "--help"]
