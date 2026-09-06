# Training image for the Phase 6 modelling pipeline (GH-57).
#
# Training only — no Airflow, no MLflow, no serving. The point is a reproducible
# environment: `uv sync --locked` pins every package to uv.lock, so a run inside the
# container is the same run tomorrow.
#
# Build:  docker compose -f docker-compose.modeling.yml build trainer
# Run:    docker compose -f docker-compose.modeling.yml run --rm trainer \
#           train -c src/modeling/configs/lgbm_xs_excess_5d.yaml
#
# See docs/operations/training-container.md.

FROM python:3.12-slim

# uv comes from its own published image rather than a curl | sh, and is pinned.
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# LightGBM's wheel links against OpenMP, which python:3.12-slim does not ship:
# without libgomp1 `import lightgbm` dies on libgomp.so.1.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so the ~400 MB ML layer is cached and only rebuilds when the
# lockfile moves. --no-build mirrors CI: every modeling dependency ships a wheel.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-build --group modeling --no-install-project

# Then the source, which changes on every commit.
COPY src/ ./src/
COPY main.py ./

# Non-root. models/ and data/ are bind mounts owned by the host user, so the
# container user needs a matching uid — override with `user:` in compose if yours
# is not 1000 (docs/operations/training-container.md).
RUN useradd --create-home --uid 1000 aurum && chown -R aurum:aurum /app
USER aurum

ENTRYPOINT ["python", "-m", "src.modeling.cli"]
CMD ["--help"]
