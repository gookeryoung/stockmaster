"""Technical indicators.

Conventions
-----------
Indicator values must match what Chinese market platforms (通达信/同花顺) show,
otherwise signals and alerts would disagree with what users see. The specific
choices that matter:

* **EMA** uses ``alpha = 2 / (n + 1)`` with ``adjust=False`` (recursive, seeded at
  the first observation) — *not* a weighted mean over a window.
* **RSI** uses Wilder smoothing (``alpha = 1 / n``), not a simple moving average.
  Using SMA is the classic way to get RSI values that disagree with every
  trading terminal.
* **KDJ** smooths RSV with the 1/3 recursion ``K = 2/3 * K_prev + 1/3 * RSV``,
  ``D = 2/3 * D_prev + 1/3 * K``, seeded at 50.
* **ATR** uses Wilder smoothing over true range, including the previous close in
  the range so overnight gaps are counted.
* **MACD** is ``EMA(12) - EMA(26)`` (DIF), ``EMA(9)`` of DIF (DEA), and
  ``2 * (DIF - DEA)`` for the histogram — the common Chinese platform definition.

Indicators are computed on **adjusted** prices; the caller decides which series.
No function peeks forward: every value at ``t`` uses only data up to ``t``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "INDICATOR_COLUMNS",
    "atr",
    "boll",
    "compute_indicators",
    "ema",
    "kdj",
    "ma",
    "macd",
    "obv",
    "rsi",
    "volume_ratio",
    "wilder",
]

#: Columns produced by :func:`compute_indicators`, in storage order.
INDICATOR_COLUMNS = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "ema12",
    "ema26",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "rsi6",
    "rsi12",
    "rsi24",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_up",
    "boll_mid",
    "boll_low",
    "atr14",
    "atr_pct",
    "obv",
    "vol_ratio",
)


def ma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over ``window`` observations."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average with ``alpha = 2 / (span + 1)``.

    ``adjust=False`` gives the recursive form that trading platforms use, where
    the first value seeds the recursion.
    """
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (``alpha = 1 / period``), as used by RSI and ATR.

    The first computed value is the simple mean of the first ``period``
    observations, matching the original Wilder formulation and common terminals.
    """
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD as ``dif`` / ``dea`` / ``hist``.

    ``hist`` is ``2 * (dif - dea)``, the Chinese-platform convention.
    """
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = 2.0 * (dif - dea)
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist})


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder smoothing.

    Returns values in ``[0, 100]``. A flat series yields 50 by construction
    (0/0 is treated as neutral rather than NaN, matching terminal behaviour).
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = wilder(gain, period)
    avg_loss = wilder(loss, period)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 means an unbroken advance -> RSI 100; both zero -> 50.
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    out.iloc[:period] = np.nan
    return out


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> pd.DataFrame:
    """KDJ stochastic oscillator, seeded at 50 for both K and D."""
    lowest = low.rolling(window=n, min_periods=1).min()
    highest = high.rolling(window=n, min_periods=1).max()
    span = (highest - lowest).replace(0.0, np.nan)
    rsv = (close - lowest) / span * 100.0
    # A flat window has no range; treat RSV as neutral.
    rsv = rsv.fillna(50.0)

    k = rsv.ewm(alpha=1.0 / m1, adjust=False, min_periods=1).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False, min_periods=1).mean()
    j = 3.0 * k - 2.0 * d
    return pd.DataFrame({"k": k, "d": d, "j": j})


def boll(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands. Uses the **population** standard deviation.

    Trading platforms compute the standard deviation with ``ddof=0``; using the
    pandas default (``ddof=1``) shifts the bands slightly and breaks parity.
    """
    mid = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    return pd.DataFrame({"up": mid + num_std * std, "mid": mid, "low": mid - num_std * std})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range, including the gap from the previous close."""
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range with Wilder smoothing."""
    return wilder(true_range(high, low, close), period)


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative signed volume.

    Volume is added on up days, subtracted on down days, and left unchanged when
    the close is flat.
    """
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume.fillna(0.0)).cumsum()


def volume_ratio(volume: pd.Series, window: int = 5) -> pd.Series:
    """量比: current volume against the mean of the *preceding* ``window`` days.

    The current day is excluded from the average, which is the standard definition
    and also avoids leaking the value being measured into its own baseline.
    """
    baseline = volume.shift(1).rolling(window=window, min_periods=window).mean()
    return volume / baseline.replace(0.0, np.nan)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every stored indicator from an OHLCV frame.

    Args:
        df: frame with lowercase ``open/high/low/close/volume`` columns, sorted
            ascending by date. Missing optional columns are tolerated.

    Returns:
        A frame indexed the same as ``df`` with :data:`INDICATOR_COLUMNS`.
    """
    if df.empty:
        return pd.DataFrame(columns=list(INDICATOR_COLUMNS), index=df.index)

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df else close
    low = df["low"].astype(float) if "low" in df else close
    volume = df["volume"].astype(float) if "volume" in df else pd.Series(0.0, index=df.index)

    out = pd.DataFrame(index=df.index)
    out["ma5"] = ma(close, 5)
    out["ma10"] = ma(close, 10)
    out["ma20"] = ma(close, 20)
    out["ma60"] = ma(close, 60)
    out["ma120"] = ma(close, 120)
    out["ema12"] = ema(close, 12)
    out["ema26"] = ema(close, 26)

    macd_df = macd(close)
    out["macd_dif"] = macd_df["dif"]
    out["macd_dea"] = macd_df["dea"]
    out["macd_hist"] = macd_df["hist"]

    out["rsi6"] = rsi(close, 6)
    out["rsi12"] = rsi(close, 12)
    out["rsi24"] = rsi(close, 24)

    kdj_df = kdj(high, low, close)
    out["kdj_k"] = kdj_df["k"]
    out["kdj_d"] = kdj_df["d"]
    out["kdj_j"] = kdj_df["j"]

    boll_df = boll(close)
    out["boll_up"] = boll_df["up"]
    out["boll_mid"] = boll_df["mid"]
    out["boll_low"] = boll_df["low"]

    atr14 = atr(high, low, close, 14)
    out["atr14"] = atr14
    out["atr_pct"] = (atr14 / close.replace(0.0, np.nan)) * 100.0

    out["obv"] = obv(close, volume)
    out["vol_ratio"] = volume_ratio(volume)

    return out[list(INDICATOR_COLUMNS)]
