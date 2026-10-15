"""Cross-sectional factor engine.

Design decisions
----------------
* **Raw factors** (``mom_20``, ``vol_20``, …) are stored as computed from data up
  to ``t``. They are the durable source of truth.
* **Standardised factors** (``z_*``) and percentile ranks (``pct_*``) are
  *cross-sectional*: they depend on the full universe on a given date. Because
  full-market backfill proceeds incrementally, such transforms are only
  meaningful once a date's cross-section is complete. They are therefore written
  in a separate step (:func:`standardize_cross_section`) that operates on
  whole dates, never on a partially populated date.
* The composite ``score`` is a transparent weighted blend of standardised
  factors. It is a ranking heuristic, **not** a trained model — the trained
  prediction path lives in :mod:`app.model`.

Every factor at ``t`` uses only data at or before ``t``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "RAW_FACTOR_COLUMNS",
    "ZSCORE_COLUMNS",
    "PCT_COLUMNS",
    "DEFAULT_SCORE_WEIGHTS",
    "compute_factors",
    "standardize_cross_section",
    "composite_score",
    "screen_frame",
]

#: Factor columns computed per symbol from its own history.
RAW_FACTOR_COLUMNS = (
    "mom_5", "mom_20", "mom_60", "mom_120", "dist_high_252",
    "vol_20", "vol_60", "atr_pct",
    "turnover_20", "amount_log_20", "amihud_20",
    "ma_bull", "breakout_20", "rsi_14",
)

#: Cross-sectional z-scores written by :func:`standardize_cross_section`.
ZSCORE_COLUMNS = ("z_mom_20", "z_mom_60", "z_vol_20", "z_turnover_20", "z_rsi_14")

#: Cross-sectional percentile ranks.
PCT_COLUMNS = ("pct_mom_20", "pct_mom_60")

#: Weights for the transparent composite score.
#:
#: Momentum is favoured, volatility penalised (low-volatility anomaly), and
#: turnover mildly positive to prefer liquid names. Signs are explicit so the
#: score's meaning is auditable.
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "z_mom_20": 0.30,
    "z_mom_60": 0.30,
    "z_vol_20": -0.20,
    "z_turnover_20": 0.10,
    "z_rsi_14": 0.10,
}


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Element-wise division treating a zero denominator as NaN."""
    return numerator / denominator.replace(0.0, np.nan)


def _pct_change_over(close: pd.Series, window: int) -> pd.Series:
    """Return over exactly ``window`` trading days, in percent."""
    return (close / close.shift(window) - 1.0) * 100.0


def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every raw factor from a symbol's OHLCV history.

    Args:
        df: ascending OHLCV frame with lowercase ``open/high/low/close/volume``
            columns. ``amount`` and ``turn`` are optional; factors derived from
            them are NaN when absent.

    Returns:
        Frame indexed like ``df`` with :data:`RAW_FACTOR_COLUMNS`.
    """
    if df.empty:
        return pd.DataFrame(columns=list(RAW_FACTOR_COLUMNS), index=df.index)

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df else close
    low = df["low"].astype(float) if "low" in df else close
    volume = (
        df["volume"].astype(float)
        if "volume" in df
        else pd.Series(np.nan, index=df.index)
    )
    amount = df["amount"].astype(float) if "amount" in df else pd.Series(np.nan, index=df.index)
    turn = df["turn"].astype(float) if "turn" in df else pd.Series(np.nan, index=df.index)

    out = pd.DataFrame(index=df.index)

    # --- momentum ---
    out["mom_5"] = _pct_change_over(close, 5)
    out["mom_20"] = _pct_change_over(close, 20)
    out["mom_60"] = _pct_change_over(close, 60)
    out["mom_120"] = _pct_change_over(close, 120)

    # Distance from the trailing 52-week high: <= 0, 0 means at the high.
    rolling_high = high.rolling(window=252, min_periods=60).max()
    out["dist_high_252"] = _safe_div(close - rolling_high, rolling_high) * 100.0

    # --- volatility ---
    returns = close.pct_change()
    out["vol_20"] = returns.rolling(window=20, min_periods=20).std(ddof=0) * np.sqrt(252)
    out["vol_60"] = returns.rolling(window=60, min_periods=60).std(ddof=0) * np.sqrt(252)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr14 = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["atr_pct"] = _safe_div(atr14, close) * 100.0

    # --- liquidity ---
    out["turnover_20"] = turn.rolling(window=20, min_periods=20).mean()
    out["amount_log_20"] = np.log1p(
        amount.rolling(window=20, min_periods=20).mean().clip(lower=0.0)
    )
    # Amihud illiquidity: |return| per unit of traded value, scaled for readability.
    abs_return = returns.abs()
    daily_amihud = _safe_div(abs_return, amount)
    out["amihud_20"] = daily_amihud.rolling(window=20, min_periods=20).mean() * 1e9

    # --- technical shape ---
    ma5 = close.rolling(window=5, min_periods=5).mean()
    ma20 = close.rolling(window=20, min_periods=20).mean()
    ma60 = close.rolling(window=60, min_periods=60).mean()
    # Bullish stack: short above medium above long.
    out["ma_bull"] = (
        (ma5 > ma20).astype(float) + (ma20 > ma60).astype(float)
    ) / 2.0
    out.loc[ma60.isna(), "ma_bull"] = np.nan

    # Breakout: close above the trailing 20-day high *excluding today*.
    prior_high = high.shift(1).rolling(window=20, min_periods=20).max()
    out["breakout_20"] = np.where(close > prior_high, 1.0, 0.0)
    out.loc[prior_high.isna(), "breakout_20"] = np.nan

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = _safe_div(avg_gain, avg_loss)
    rsi14 = 100.0 - (100.0 / (1.0 + rs))
    rsi14 = rsi14.where(avg_loss != 0, 100.0)
    rsi14 = rsi14.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    out["rsi_14"] = rsi14

    return out[list(RAW_FACTOR_COLUMNS)]


def standardize_cross_section(frame: pd.DataFrame) -> pd.DataFrame:
    """Add z-scores and percentile ranks, computed **within each trade_date**.

    Args:
        frame: a *whole-date* cross-section with ``trade_date`` plus raw factor
            columns.

    Returns:
        The frame plus :data:`ZSCORE_COLUMNS` and :data:`PCT_COLUMNS`.

    A factor is z-scored only when at least two non-null observations exist;
    otherwise the score would be a division by zero rather than a ranking.
    """
    out = frame.copy()
    if out.empty:
        for column in (*ZSCORE_COLUMNS, *PCT_COLUMNS):
            out[column] = pd.Series(dtype=float)
        return out

    sources = {
        "z_mom_20": "mom_20",
        "z_mom_60": "mom_60",
        "z_vol_20": "vol_20",
        "z_turnover_20": "turnover_20",
        "z_rsi_14": "rsi_14",
    }
    for target, source in sources.items():
        if source not in out:
            out[target] = np.nan
            continue

        def _zscore(series: pd.Series) -> pd.Series:
            if series.notna().sum() < 2:
                return pd.Series(np.nan, index=series.index)
            std = series.std(ddof=0)
            if not std or np.isnan(std):
                return pd.Series(np.nan, index=series.index)
            return (series - series.mean()) / std

        out[target] = out.groupby("trade_date", group_keys=False)[source].apply(_zscore)

    for target, source in (("pct_mom_20", "mom_20"), ("pct_mom_60", "mom_60")):
        if source not in out:
            out[target] = np.nan
            continue
        out[target] = out.groupby("trade_date", group_keys=False)[source].rank(pct=True)

    return out


def composite_score(
    frame: pd.DataFrame, weights: dict[str, float] | None = None
) -> pd.Series:
    """Weighted blend of standardised factors, renormalised per row.

    Rows missing some factors still receive a score from the factors they do
    have, with weights renormalised over the available columns. A row with no
    usable factor yields NaN rather than a misleading zero.
    """
    weights = weights or DEFAULT_SCORE_WEIGHTS
    if frame.empty:
        return pd.Series(dtype=float, index=frame.index)

    weighted_sum = pd.Series(0.0, index=frame.index)
    weight_total = pd.Series(0.0, index=frame.index)

    for column, weight in weights.items():
        if column not in frame:
            continue
        values = frame[column].astype(float)
        present = values.notna()
        weighted_sum = weighted_sum.add(values.fillna(0.0) * weight, fill_value=0.0)
        weight_total = weight_total.add(present.astype(float) * abs(weight), fill_value=0.0)

    score = weighted_sum / weight_total.replace(0.0, np.nan)
    return score


def screen_frame(
    frame: pd.DataFrame,
    *,
    min_score: float | None = None,
    max_vol: float | None = None,
    min_amount_log: float | None = None,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """Apply simple, explicit filters to a factor frame.

    Kept deliberately narrow: complex screening belongs in the API layer where
    filters can be validated and explained back to the user.
    """
    out = frame.copy()
    if exclude_st and "is_st" in out:
        out = out[~out["is_st"].fillna(False).astype(bool)]
    if min_score is not None and "score" in out:
        out = out[out["score"] >= min_score]
    if max_vol is not None and "vol_20" in out:
        out = out[out["vol_20"].isna() | (out["vol_20"] <= max_vol)]
    if min_amount_log is not None and "amount_log_20" in out:
        out = out[out["amount_log_20"].isna() | (out["amount_log_20"] >= min_amount_log)]
    return out
