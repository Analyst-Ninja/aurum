"""Write ``seeds/selected_features.csv`` and the narrowed run config.

The seed is the only thing in this repo where a modelling artifact reaches into the
warehouse, so both failure modes are checked here rather than trusted:

* **A target column in the seed** silently poisons every model trained afterwards.
  This raises rather than filtering, because a `fwd_ret_*` reaching this function
  means the ranking upstream is already wrong and quietly dropping it hides that.
* **Zero selected rows** compiles `mart_feature_summary` to ``select  from …`` — a
  bare SQL syntax error with nothing in it pointing back to this file.
"""

import json
import logging
import re
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# The seed's schema, in order. `mart_feature_summary` reads `feature_name`, `selected`
# and `rank`; `mean_abs_shap` and `model_version` are there for the human reviewing the
# diff. See `docs/warehouse/rationale/selected-features-seed.md` §3.
SEED_COLUMNS = ["feature_name", "rank", "mean_abs_shap", "selected", "model_version"]

TARGET_PATTERN = re.compile(r"^(fwd_ret|label_)")
FORBIDDEN = {"fold_id"}

COMPARISON = "comparison.json"


def write_seed(ranking: pd.DataFrame, model_version: str, path: Path) -> Path:
    """Replace the seed with `ranking`. Every feature is written, ranked."""
    leaked = [
        name
        for name in ranking["feature_name"]
        if TARGET_PATTERN.match(name) or name in FORBIDDEN
    ]
    if leaked:
        raise ValueError(f"Target columns reached the feature ranking: {leaked}")

    selected = ranking["selected"].astype(bool)
    if not selected.any():
        raise ValueError(
            "Refusing to write a seed with zero selected features — "
            "mart_feature_summary would compile to invalid SQL"
        )

    seed = ranking.assign(
        model_version=model_version,
        selected=selected.map({True: "true", False: "false"}),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    seed[SEED_COLUMNS].to_csv(path, index=False)
    logger.info(
        "Wrote %s (%s features, %s selected)", path, len(seed), int(selected.sum())
    )
    return path


def write_narrow_config(base_config: Path, ranking: pd.DataFrame) -> Path:
    """Write the sibling config that trains on the selected features only.

    A second file rather than a flag: the narrowed run has to be reproducible from a
    config alone, and #55's gate is a comparison between two runs that must differ in
    exactly one documented way.
    """
    payload = yaml.safe_load(base_config.read_text())
    features = ranking.loc[ranking["selected"].astype(bool), "feature_name"].tolist()
    payload.setdefault("preprocess", {})["allow_list"] = features

    path = base_config.with_name(f"{base_config.stem}_narrow{base_config.suffix}")
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def _headline(metrics: dict) -> dict:
    """The two numbers #55's gate is decided on, from a `metrics.json`."""
    holdout = metrics["holdout"]["model"]
    return {
        "ic": holdout["ic"]["mean"],
        "icir": holdout["icir"],
        "decile_spread": holdout["decile_spread"]["mean"],
    }


def compare_feature_sets(full: Path, narrow: Path) -> dict:
    """Compare a narrowed run against the full-feature baseline on the holdout.

    Feature selection is a hypothesis, not an improvement: if the narrowed model does
    not match or beat the baseline on both ICIR and decile spread, the seed does not
    get committed. Writes `comparison.json` beside the narrowed run's metrics.
    """
    baseline = _headline(json.loads(full.read_text()))
    candidate = _headline(json.loads(narrow.read_text()))
    wins = (
        candidate["icir"] >= baseline["icir"]
        and candidate["decile_spread"] >= baseline["decile_spread"]
    )

    report = {
        "full": {"metrics_path": str(full), **baseline},
        "narrowed": {"metrics_path": str(narrow), **candidate},
        "narrowed_wins": bool(wins),
        "verdict": (
            "Commit the seed."
            if wins
            else "Do not commit the seed — the narrowed model lost on the holdout."
        ),
    }
    path = narrow.parent / COMPARISON
    path.write_text(json.dumps(report, indent=2))
    logger.info("%s Wrote %s", report["verdict"], path)
    return report
