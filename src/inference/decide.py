"""Scores to decisions. Pure functions: no database, no filesystem.

The model ranks stocks against each other on a given day; it does not forecast a price.
So the decision is a position in that day's ranking, and it reuses ``metrics.deciles``
— the definition the warehouse, the evaluation harness and the backtest already share —
which is what lets a backtest of the book speak for the decisions emitted here.
"""

from typing import Any

import numpy as np
import pandas as pd

from src.modeling.config import PreprocessConfig
from src.modeling.data.preprocess import add_indicators, build_features
from src.modeling.evaluate.metrics import N_DECILES, deciles

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"


def score_frame(
    booster: Any,
    manifest: dict[str, Any],
    frame: pd.DataFrame,
    preprocess: PreprocessConfig,
) -> pd.DataFrame:
    """``symbol, date, score`` for every row of ``frame``.

    Replays the stored feature manifest through ``build_features``; a moved or missing
    column raises there rather than producing confident nonsense here.

    The allow-list comes from the manifest, not from ``preprocess``. A narrowed run's list
    is chosen by SHAP at training time, so the ``_narrow.yaml`` in the repo can be a
    different (older) selection than the model that was trained and published.
    """
    preprocess = preprocess.model_copy(update={"allow_list": manifest.get("allow_list")})
    frame = add_indicators(frame)
    matrix, _ = build_features(frame, preprocess, manifest=manifest)
    return frame[["symbol", "date"]].assign(score=booster.predict(matrix))


def assign_actions(scores: pd.DataFrame) -> pd.DataFrame:
    """Add ``decile``, ``action`` and ``rank`` to ``symbol, date, score``.

    Top decile BUY, bottom decile SELL, everything between HOLD. A null score has no
    decile and is HOLD: absent a ranking there is nothing to act on. SELL means "avoid /
    underweight against the market", not a call that the price falls.
    """
    out = scores.copy()
    out["decile"] = deciles(out["score"].to_numpy(), out["date"].to_numpy()).to_numpy()
    out["action"] = np.select(
        [out["decile"] == N_DECILES, out["decile"] == 1], [BUY, SELL], default=HOLD
    )
    out["rank"] = (
        out.groupby("date", observed=True)["score"]
        .rank(ascending=False, method="first")
        .astype("Int64")
    )
    return out.sort_values(["date", "rank"], ignore_index=True)
