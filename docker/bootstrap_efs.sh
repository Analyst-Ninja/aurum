#!/bin/sh
# One-off: seed the EFS volume with the files the weekly pipeline needs to persist
# between ECS tasks. Run once after the first `terraform apply` (GH-74), before the
# schedules exist (GH-75).
#
#   aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"aurum",
#     "command":["sh","docker/bootstrap_efs.sh"]}]}'
#
# Why this exists: `select-features` writes the generated `<config>_narrow.yaml` beside
# the base config, and that path is derived from the base config's path rather than
# configurable. Every Step Functions state is a fresh container, so a base config baked
# into the image takes the generated narrow config down with it when the task exits.
# Keeping the base config on EFS makes the generated sibling land on EFS too.
set -eu

CONFIG_DIR=/app/models/configs
SEED_DIR=/app/models/seeds
BASE_CONFIG=src/modeling/configs/lgbm_xs_excess_5d.yaml
REPO_SEED=src/transformation/aurum_dwh/seeds/selected_features.csv

mkdir -p "$CONFIG_DIR" "$SEED_DIR"

# The EFS copy differs from the committed config in exactly one field: the seed lands on
# EFS as well, so the CSV survives the task that produced it.
python - "$BASE_CONFIG" "$CONFIG_DIR/lgbm_xs_excess_5d.yaml" "$SEED_DIR/selected_features.csv" <<'PY'
import sys
import pathlib
import yaml

source, destination, seed_path = (pathlib.Path(a) for a in sys.argv[1:4])
config = yaml.safe_load(source.read_text())
config.setdefault("select", {})["seed_path"] = str(seed_path)
destination.write_text(yaml.safe_dump(config, sort_keys=False))
print(f"wrote {destination} (select.seed_path -> {seed_path})")
PY

# The committed seed is the starting point; select-features replaces it each week.
if [ ! -f "$SEED_DIR/selected_features.csv" ]; then
    cp "$REPO_SEED" "$SEED_DIR/selected_features.csv"
    echo "seeded $SEED_DIR/selected_features.csv from the repo"
else
    echo "$SEED_DIR/selected_features.csv already present, left alone"
fi

echo
echo "EFS bootstrap complete:"
ls -l "$CONFIG_DIR" "$SEED_DIR"
