"""Cross-sectional score history for backtesting.

Why this exists
---------------
The ``factors`` table is an intentionally *thin* cache: the analytics job stores
only the most recent row per symbol, because its purpose is to power the screening
and factor-snapshot endpoints. A backtest, however, needs a score **per date** so
it can rebalance over time.

Rather than bloating storage with a full factor history for the whole market, the
engine recomputes the score series on demand from the stored bars. That trades a
little CPU for a much smaller database, and it keeps a single source of truth for
the factor definitions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.factors.compute import RAW_FACTOR_COLUMNS, composite_score, compute_factors, standardize_cross_section

log = get_logger(__name__)

__all__ = ["build_score_history", "factor_frame_for_history"]


def factor_frame_for_history(bars_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compute raw factors for every (code, date) pair.

    Returns a long frame with ``trade_date``, ``code`` and the raw factor columns.
    """
    frames: list[pd.DataFrame] = []
    for code, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue
        df = bars.sort_values("trade_date").reset_index(drop=True)
        if len(df) < 60:
            # Fewer bars than the longest factor window; nothing usable.
            continue
        factors = compute_factors(df)
        factors.insert(0, "code", code)
        factors.insert(0, "trade_date", df["trade_date"].values)
        frames.append(factors)

    if not frames:
        return pd.DataFrame(columns=["trade_date", "code", *RAW_FACTOR_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def build_score_history(bars_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Score every symbol on every date, cross-sectionally standardised.

    Args:
        bars_by_symbol: canonical code -> ascending OHLCV frame.

    Returns:
        Frame with ``trade_date``, ``code`` and ``score``, sorted by
        ``(trade_date, code)``. Dates where fewer than two symbols have factors
        are dropped, since a cross-sectional z-score is undefined for a single
        name.
    """
    raw = factor_frame_for_history(bars_by_symbol)
    if raw.empty:
        return pd.DataFrame(columns=["trade_date", "code", "score"])

    raw = raw.dropna(subset=["trade_date"])
    if raw.empty:
        return pd.DataFrame(columns=["trade_date", "code", "score"])

    # Drop dates with no cross-section: standardising a single row is meaningless.
    counts = raw.groupby("trade_date")["code"].transform("count")
    raw = raw[counts >= 2]
    if raw.empty:
        log.info("score history empty: no date had two or more symbols with factors")
        return pd.DataFrame(columns=["trade_date", "code", "score"])

    standardized = standardize_cross_section(raw)
    standardized["score"] = composite_score(standardized)

    out = standardized[["trade_date", "code", "score"]].dropna(subset=["score"])
    return out.sort_values(["trade_date", "code"]).reset_index(drop=True)


def top_n_per_date(score_history: pd.DataFrame, top_n: int) -> dict:
    """Map each date to an equal-weight portfolio of the best-scoring names."""
    if score_history.empty or top_n < 1:
        return {}
    signals: dict = {}
    for day, group in score_history.groupby("trade_date"):
        ranked = group.sort_values("score", ascending=False).head(top_n)
        codes = [c for c in ranked["code"].tolist() if isinstance(c, str)]
        if not codes:
            continue
        weight = 1.0 / len(codes)
        signals[day] = {code: weight for code in codes}
    return signals


def summarize_scores(score_history: pd.DataFrame) -> dict:
    """Coverage summary, for honest reporting when history is thin."""
    if score_history.empty:
        return {"dates": 0, "symbols": 0, "rows": 0}
    return {
        "dates": int(score_history["trade_date"].nunique()),
        "symbols": int(score_history["code"].nunique()),
        "rows": int(len(score_history)),
        "first_date": str(score_history["trade_date"].min()),
        "last_date": str(score_history["trade_date"].max()),
    }
