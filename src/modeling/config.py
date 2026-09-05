"""Run configuration for the modelling subsystem.

One YAML fully specifies a run, mirroring the ingestion framework. Credentials are
env var *names*, resolved at connect time from the repo-root ``.env``.

Every model forbids extra keys, so a mistyped one fails at load with the offending
key named rather than being silently ignored.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.utils.config_reader import read_config


class SourceConfig(BaseModel):
    """The warehouse table a run reads from."""

    model_config = ConfigDict(extra="forbid")

    db_schema: str
    table: str
    # Inference reads the feature mart, which by construction has no target columns —
    # that is what makes the manifest replay a real check rather than a formality.
    predict_table: str = "mart_features"
    db_name: str
    # Env var NAMES, not values.
    host: str
    port: str
    username: str
    password: str


class CacheConfig(BaseModel):
    """Local Parquet cache of the source table."""

    model_config = ConfigDict(extra="forbid")

    dir: Path = Path("data/training")
    enabled: bool = True


# Columns a model may never see. Explicit, never inferred from a suffix or prefix —
# the panel contains two traps that make pattern matching wrong:
#   * 37 columns end in `_decile`; the 37th is `fwd_ret_5d_xs_decile`, a target.
#   * 12 columns start with `market_`, but only the 7 listed below are date-constant.
#     `market_cap_z`, `market_cap_decile`, `market_cap_vs_sector` and
#     `market_corr_252d` are per-symbol features a prefix rule would wrongly drop.
LEAKAGE_COLUMNS = [
    "fwd_ret_5d",
    "fwd_ret_21d",
    "fwd_ret_5d_excess",
    "fwd_ret_5d_xs_decile",
    "label_up_5d",
    "fold_id",
]

# `symbol` and `date` index the panel. The two fiscal timestamps are dropped because
# they are datetimes — `days_since_available` already carries the staleness numerically.
# `period_type` describes the filing, not the company.
IDENTIFIER_COLUMNS = [
    "symbol",
    "date",
    "period_type",
    "fiscal_period_end",
    "fundamental_available_from",
]

# Absolute levels. A tree splits on a threshold, and `market_cap > 5e10` means
# top-decile in 2001 and mid-cap in 2026 — across a 26-year panel that split encodes
# *when*, not a relationship. Every one has a `_z` / `_decile` / `_vs_sector`
# counterpart in GOLD that says the same thing as a position within that day's
# universe, and those are kept. The market-level series are excluded for a different
# reason: they are identical across all symbols on a date, so they carry zero
# ranking information.
NON_STATIONARY_COLUMNS = [
    "adj_close",
    "close_raw",
    "market_cap",
    "enterprise_value",
    "revenue",
    "net_income",
    "total_assets",
    "total_equity",
    "total_debt",
    "shares_outstanding",
    "adv_21d",
    "dollar_volume",
    "ma_10",
    "ma_20",
    "ma_50",
    "ma_200",
    "high_252d",
    "low_252d",
    "market_ret_1d",
    "market_ret_21d",
    "market_ret_63d",
    "market_vol_21d",
    "market_vol_63d",
    "market_breadth",
    "market_xs_dispersion",
]

CATEGORICAL_COLUMNS = ["sector", "industry"]


class PreprocessConfig(BaseModel):
    """Row filters, column policy and target transforms.

    Deliberately thin: GOLD already winsorizes, z-scores, deciles and sector-centres
    per date, and this layer must not repeat any of it.
    """

    model_config = ConfigDict(extra="forbid")

    # Drop each symbol's first year. Features built on a 252-row window are computed
    # over a partial window before that, and the missingness correlates with
    # recently-listed companies — a real but era-specific pattern.
    warmup_bars: int = 252
    # A per-date z-score or decile over a handful of names is noise. Drops nothing on
    # the current universe (the thinnest date carries 311 symbols); it is a guard.
    min_cross_section: int = 100
    winsorize_lower: float = 0.01
    winsorize_upper: float = 0.99
    standardize_per_date: bool = True

    leakage_columns: list[str] = LEAKAGE_COLUMNS
    identifier_columns: list[str] = IDENTIFIER_COLUMNS
    non_stationary_columns: list[str] = NON_STATIONARY_COLUMNS
    categorical_columns: list[str] = CATEGORICAL_COLUMNS


class SplitConfig(BaseModel):
    """Purged, embargoed, expanding walk-forward."""

    model_config = ConfigDict(extra="forbid")

    # Label horizon in trading days. Consecutive rows share four of five label days,
    # so training rows this close to a validation window overlap it.
    horizon: int = 5
    # Extra days dropped *after* a validation window. Under an expanding window a
    # later fold trains on the period right after an earlier validation window, and
    # serial correlation carries information backwards across that boundary.
    embargo: int = 10
    # 321 monthly folds: 1-120 burn-in, 121-297 evaluation, 298-321 holdout.
    burn_in_folds: int = 120
    eval_end_fold: int = 297
    refit_every: int = 12
    # Half-life in years for sample-weight decay. None = uniform weights.
    decay_half_life_years: float | None = None


class ModelParams(BaseModel):
    """One LightGBM configuration.

    The defaults regularize far harder than the library's. `min_child_samples=20`
    would let a leaf fit 20 rows out of 2.7M on a target whose R2 is single-digit
    basis points, and `feature_fraction` is low because the `_z` / `_decile` /
    `_vs_sector` triplets are heavily collinear — sampling columns decorrelates the
    trees.
    """

    model_config = ConfigDict(extra="forbid")

    objective: str = "regression"
    num_leaves: int = 31
    min_child_samples: int = 2000
    learning_rate: float = 0.03
    feature_fraction: float = 0.4
    bagging_fraction: float = 0.7
    bagging_freq: int = 1
    lambda_l2: float = 10.0
    # A cap, not a target — early stopping on IC decides where to stop.
    n_estimators: int = 1500
    early_stopping_rounds: int = 100
    verbosity: int = -1
    seed: int = 42

    # Resource limits, tuned for an Apple Silicon laptop rather than a server.
    #
    # LightGBM has no Metal backend — its GPU support is OpenCL and CUDA only, and the
    # PyPI wheel is CPU-only regardless — so the M-series GPU cannot be used here. The
    # levers that do work are threads and bin count.
    #
    # num_threads counts *performance* cores, not all cores. An M4 has 4P + 6E; every
    # boosting iteration ends at a barrier, so scheduling work onto the slow E-cores
    # makes all ten threads wait on the slowest. Four is usually faster than ten, and
    # markedly cooler.
    num_threads: int = 4
    # 63 rather than the default 255. The binned dataset is the dominant allocation,
    # and a quarter of the bins is a quarter of the memory plus faster histograms. The
    # features are already z-scores, deciles and ratios, so finer bins buy little on a
    # target this noisy.
    max_bin: int = 63
    # Skip LightGBM's row-wise/col-wise auto-detection, which builds both and measures.
    force_col_wise: bool = True


class TrainConfig(BaseModel):
    """The hyperparameter search, such as it is.

    A list, so widening the search is a YAML edit. It stays at one entry by default:
    the design defers Optuna because a large search over a noisy objective
    manufactures overfitting and inflates `n_configs_tried`, which then deflates
    every Sharpe reported downstream.
    """

    model_config = ConfigDict(extra="forbid")

    grid: list[ModelParams] = [ModelParams()]


class ModelingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceConfig
    target: str
    cache: CacheConfig = CacheConfig()
    preprocess: PreprocessConfig = PreprocessConfig()
    splits: SplitConfig = SplitConfig()
    train: TrainConfig = TrainConfig()
    # Manifests land here. GH-53's registry moves them under a version directory.
    output_dir: Path = Path("models")


def load_config(config_path: str | Path) -> ModelingConfig:
    """Read and validate a run config."""
    return ModelingConfig(**read_config(Path(config_path)))
