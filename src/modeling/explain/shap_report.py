"""Tree SHAP over a saved run, collinearity pruning, and the selection cutoff.

Three choices worth stating up front, because each departs from the letter of #55.

**Contributions come from ``booster.predict(pred_contrib=True)``, not the ``shap``
package.** For a LightGBM model, ``shap.TreeExplainer`` delegates to exactly this
call — it is the same exact-Tree-SHAP implementation, reached without a 60 MB
dependency, and it takes the frame through the same code path as ``predict``, so the
``category`` dtypes on ``sector``/``industry`` are handled identically rather than
needing a numeric re-encode.

**Stability is measured across era blocks, not across folds.** #55 asks for mean|SHAP|
per walk-forward refit with the std across the ~15 of them. Those boosters do not
exist: ``fit_fold`` deletes each one (``lgbm.py``) and ``build_dataset`` frees the raw
matrix, so per-fold SHAP means fifteen retrains. Instead the sample is split into
contiguous chronological blocks of the *one* shipped booster and the std is taken
across those. It answers a slightly different question — "is this feature important in
every era" rather than "in every refit" — and that is the question the std was wanted
for. ``per_fold.csv`` carries the block detail and names its blocks by date range so
the substitution is visible in the artifact, not only here.

**The sample is per-date, not uniform.** The universe grows from ~320 names in 2000 to
~500 in 2026, so a uniform draw over rows quietly weights the ranking toward recent
years.
"""

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.modeling.config import ModelingConfig, SelectConfig, load_config
from src.modeling.evaluate.runner import _panel
from src.modeling.explain.seed_writer import write_narrow_config, write_seed
from src.modeling.models.registry import load_run

logger = logging.getLogger(__name__)

RANKING = "ranking.csv"
PER_FOLD = "per_fold.csv"


def stratified_sample(dates: pd.Series, n_rows: int, seed: int) -> np.ndarray:
    """Positional indices, an equal quota drawn from every date.

    Returns positions into `dates`, sorted, so callers can slice a matrix with `.iloc`.
    """
    rng = np.random.default_rng(seed)
    positions = np.arange(len(dates))
    groups = pd.Series(positions).groupby(dates.to_numpy())
    quota = max(1, n_rows // max(1, dates.nunique()))

    picked = [
        rng.choice(group.to_numpy(), size=min(quota, len(group)), replace=False)
        for _, group in groups
    ]
    return np.sort(np.concatenate(picked)) if picked else np.array([], dtype=int)


def era_blocks(dates: pd.Series, n_blocks: int) -> list[np.ndarray]:
    """Split positions into `n_blocks` contiguous chronological blocks.

    Split on *dates* rather than rows so a block boundary never cuts a cross-section
    in half — one date's SHAP values belong to one block.
    """
    unique = np.sort(dates.unique())
    if len(unique) == 0:
        return []
    edges = np.array_split(unique, min(n_blocks, len(unique)))
    values = dates.to_numpy()
    return [np.flatnonzero(np.isin(values, edge)) for edge in edges if len(edge)]


def block_contributions(booster: Any, matrix: pd.DataFrame) -> np.ndarray:
    """Mean |SHAP| per feature over `matrix`.

    `pred_contrib` returns one column per feature plus a trailing bias column, which
    is dropped — the base value is not an attribution.
    """
    contributions = booster.predict(matrix, pred_contrib=True)
    return np.abs(np.asarray(contributions)[:, :-1]).mean(axis=0)


def shap_ranking(
    booster: Any, matrix: pd.DataFrame, dates: pd.Series, n_blocks: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return `(ranking, per_block)`.

    `ranking` carries `feature_name, mean_abs_shap, std_abs_shap`, sorted descending.
    `per_block` is the long-form detail behind the std.
    """
    blocks = era_blocks(dates, n_blocks)
    features = list(matrix.columns)

    rows = []
    for index, positions in enumerate(blocks):
        block_dates = dates.iloc[positions]
        means = block_contributions(booster, matrix.iloc[positions])
        logger.info(
            "Block %s: %s rows, %s to %s",
            index,
            f"{len(positions):,}",
            block_dates.min().date(),
            block_dates.max().date(),
        )
        rows.append(
            pd.DataFrame(
                {
                    "feature_name": features,
                    "block_index": index,
                    "block_start": str(block_dates.min().date()),
                    "block_end": str(block_dates.max().date()),
                    "mean_abs_shap": means,
                }
            )
        )

    per_block = pd.concat(rows, ignore_index=True)
    grouped = per_block.groupby("feature_name")["mean_abs_shap"]
    ranking = (
        pd.DataFrame(
            {"mean_abs_shap": grouped.mean(), "std_abs_shap": grouped.std(ddof=0)}
        )
        .reset_index()
        .sort_values("mean_abs_shap", ascending=False, ignore_index=True)
    )
    return ranking, per_block


def cluster_by_correlation(
    matrix: pd.DataFrame, ranking: pd.DataFrame, threshold: float
) -> pd.Series:
    """Single-linkage cluster ids over |Spearman rho| > `threshold`.

    Indexed by feature name. Non-numeric columns are their own cluster: a `category`
    has no rank correlation, and it cannot be the collinear duplicate the pruning is
    aimed at.
    """
    order = ranking["feature_name"].tolist()
    numeric = matrix.select_dtypes("number")
    parent = {name: name for name in order}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    if not numeric.empty:
        correlation = numeric.corr(method="spearman").abs()
        columns = correlation.columns
        pairs = np.argwhere(np.triu(correlation.to_numpy() > threshold, k=1))
        for left, right in pairs:
            a, b = find(columns[left]), find(columns[right])
            if a != b:
                parent[a] = b

    # Number clusters by the rank of their strongest member, so cluster 1 leads the
    # ranking and a diff of `ranking.csv` between runs stays readable.
    labels, ids = {}, {}
    for name in order:
        root = find(name)
        if root not in labels:
            labels[root] = len(labels) + 1
        ids[name] = labels[root]
    return pd.Series(ids, name="cluster_id")


def apply_cutoff(
    ranking: pd.DataFrame, cum_share: float, max_features: int
) -> tuple[pd.DataFrame, str]:
    """Mark `selected` on cluster leaders up to a cumulative share, capped.

    Only the highest-SHAP member of a cluster is eligible — SHAP splits credit across
    correlated features, so keeping all three of `ret_21d`, `ret_21d_z` and
    `ret_21d_decile` spends the budget three times on one signal. Dropped members stay
    in the file with `selected = false`; truncating discards exactly the information
    needed to retune the threshold.
    """
    ranking = ranking.sort_values("mean_abs_shap", ascending=False, ignore_index=True)
    ranking["rank"] = np.arange(1, len(ranking) + 1)

    leader = ~ranking["cluster_id"].duplicated()
    total = float(ranking["mean_abs_shap"].sum()) or 1.0
    share = ranking["mean_abs_shap"].where(leader, 0.0).cumsum() / total

    # `share` first crosses the bar on some row; that row is included, the next is not.
    reached = share >= cum_share
    limit = int(reached.idxmax()) if reached.any() else len(ranking) - 1
    selected = leader & (ranking.index <= limit)

    rule = "cumulative"
    if int(selected.sum()) > max_features:
        keep = ranking.index[selected][:max_features]
        selected = ranking.index.isin(keep)
        rule = "cap"

    ranking["selected"] = selected
    ranking["cutoff_rule"] = rule
    return ranking, rule


def run_select_features(config_path: str, version: str) -> Path:
    """Rank features for `version`, write the artifacts and rewrite the seed."""
    config = load_config(config_path)
    booster, manifest = load_run(config.output_dir, version)
    directory = (config.output_dir / version).resolve()
    settings: SelectConfig = config.select

    frame, matrix = _panel(config, manifest)
    evaluation = (
        (frame["fold_id"] > config.splits.burn_in_folds)
        & (frame["fold_id"] <= config.splits.eval_end_fold)
    ).to_numpy()
    if not evaluation.any():
        raise ValueError("No evaluation-window rows — nothing to explain")

    frame, matrix = frame[evaluation], matrix[evaluation]
    picked = stratified_sample(frame["date"], settings.sample_rows, config.backtest.seed)
    sample, dates = matrix.iloc[picked], frame["date"].iloc[picked]
    logger.info(
        "Explaining %s rows x %s features over %s dates",
        f"{len(sample):,}",
        sample.shape[1],
        dates.nunique(),
    )

    ranking, per_block = shap_ranking(booster, sample, dates, settings.n_era_blocks)
    ranking["cluster_id"] = (
        cluster_by_correlation(sample, ranking, settings.corr_threshold)
        .reindex(ranking["feature_name"])
        .to_numpy()
    )
    ranking, rule = apply_cutoff(ranking, settings.cum_share, settings.max_features)

    shap_dir = directory / "shap"
    shap_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "feature_name",
        "rank",
        "mean_abs_shap",
        "std_abs_shap",
        "cluster_id",
        "cutoff_rule",
        "selected",
    ]
    ranking[columns].to_csv(shap_dir / RANKING, index=False)
    per_block.to_csv(shap_dir / PER_FOLD, index=False)

    run_version = _version_of(config, version)
    write_seed(ranking, run_version, settings.seed_path)
    narrow = write_narrow_config(Path(config_path), ranking)
    logger.info(
        "%s features selected by the %s rule; wrote %s, %s and %s",
        int(ranking["selected"].sum()),
        rule,
        shap_dir / RANKING,
        settings.seed_path,
        narrow,
    )
    return shap_dir / RANKING


def _version_of(config: ModelingConfig, version: str) -> str:
    """Resolve `latest` to the directory it points at, for the seed's `model_version`."""
    return (config.output_dir / version).resolve().name
