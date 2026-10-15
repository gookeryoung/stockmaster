"""Indicator correctness tests.

The point of these is *parity with trading terminals*. A plausible-looking RSI is
worthless if it disagrees with what users see, so the tests pin the specific
conventions: recursive EMA, Wilder smoothing, ddof=0 bands, and the 2x MACD
histogram. Reference values are derived from the definitions, not from the
implementation, so a convention change fails loudly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators.compute import (
    INDICATOR_COLUMNS,
    atr,
    boll,
    compute_indicators,
    ema,
    kdj,
    ma,
    macd,
    obv,
    rsi,
    true_range,
    volume_ratio,
    wilder,
)


def frame(n: int = 200, seed: int = 7) -> pd.DataFrame:
    """A reproducible OHLCV frame with a trend, noise and no invalid bars."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.05, 1.0, n))
    close = np.maximum(close, 1.0)
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = np.abs(rng.normal(1e6, 2e5, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


class TestMovingAverages:
    def test_ma_is_simple_mean(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ma(s, 3)
        assert pd.isna(result.iloc[0]) and pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[3] == pytest.approx(3.0)
        assert result.iloc[4] == pytest.approx(4.0)

    def test_ema_matches_hand_computed_recursion(self):
        """EMA seeds at the first value, then alpha = 2/(n+1)."""
        s = pd.Series([10.0, 11.0, 12.0, 13.0])
        alpha = 2 / (3 + 1)
        expected = [10.0]
        for value in [11.0, 12.0, 13.0]:
            expected.append(expected[-1] + alpha * (value - expected[-1]))
        result = ema(s, 3)
        assert list(result) == pytest.approx(expected)

    def test_ema_first_value_equals_first_observation(self):
        s = pd.Series([42.0, 0.0, 0.0])
        assert ema(s, 5).iloc[0] == pytest.approx(42.0)


class TestWilder:
    def test_wilder_matches_hand_computed_recursion(self):
        s = pd.Series([2.0, 4.0, 6.0, 8.0])
        period = 2
        expected = [np.nan, 3.0, 3.0 + (6.0 - 3.0) / 2, 3.0 + (8.0 - 3.0) / 2]
        # Recompute step by step to avoid transcription errors.
        seed = (2.0 + 4.0) / 2
        step = seed + (6.0 - seed) / 2
        step2 = step + (8.0 - step) / 2
        expected = [np.nan, seed, step, step2]
        result = wilder(s, period)
        assert list(result) == pytest.approx(expected, nan_ok=True)


class TestRsi:
    def test_monotonic_rise_gives_100(self):
        s = pd.Series(np.arange(1.0, 40.0))
        result = rsi(s, 14)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_monotonic_fall_gives_zero(self):
        s = pd.Series(np.arange(40.0, 1.0, -1.0))
        result = rsi(s, 14)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_flat_series_is_neutral_50(self):
        s = pd.Series([5.0] * 40)
        result = rsi(s, 14)
        assert result.iloc[-1] == pytest.approx(50.0)

    def test_bounded_between_0_and_100(self):
        result = rsi(frame(300)["close"], 14).dropna()
        assert ((result >= 0) & (result <= 100)).all()

    def test_uses_wilder_not_sma(self):
        """Guard against the classic error of smoothing with a simple mean."""
        close = frame(120, seed=3)["close"]
        period = 14
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        expected = 100 - 100 / (1 + avg_gain / avg_loss)
        result = rsi(close, period)
        tail = slice(period + 5, None)
        assert list(result[tail]) == pytest.approx(list(expected[tail]), rel=1e-9)

    def test_warmup_is_nan(self):
        result = rsi(pd.Series(np.arange(1.0, 30.0)), 14)
        assert result.iloc[:14].isna().all()
        assert not np.isnan(result.iloc[14])

    def test_no_lookahead(self):
        """A value at t must not change when future data is appended."""
        close = frame(100)["close"]
        early = rsi(close.iloc[:60], 14).iloc[-1]
        full = rsi(close, 14).iloc[59]
        assert early == pytest.approx(full)


class TestMacd:
    def test_histogram_is_twice_the_dif_dea_gap(self):
        result = macd(frame(200)["close"])
        assert list(result["hist"]) == pytest.approx(list(2.0 * (result["dif"] - result["dea"])))

    def test_dif_equals_ema_difference(self):
        close = frame(150)["close"]
        result = macd(close)
        expected = ema(close, 12) - ema(close, 26)
        assert list(result["dif"]) == pytest.approx(list(expected))

    def test_dea_is_ema9_of_dif(self):
        close = frame(150)["close"]
        result = macd(close)
        expected = ema(result["dif"], 9)
        assert list(result["dea"]) == pytest.approx(list(expected))

    def test_positive_histogram_in_uptrend(self):
        close = pd.Series(np.linspace(10, 100, 120))
        result = macd(close)
        assert result["hist"].iloc[-1] > 0


class TestKdj:
    def test_matches_1_3_recursion(self):
        df = frame(120, seed=11)
        result = kdj(df["high"], df["low"], df["close"])
        n = 9
        lowest = df["low"].rolling(window=n, min_periods=1).min()
        highest = df["high"].rolling(window=n, min_periods=1).max()
        span = (highest - lowest).replace(0.0, np.nan)
        rsv = ((df["close"] - lowest) / span * 100.0).fillna(50.0)
        k = rsv.ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()
        d = k.ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()
        assert list(result["k"]) == pytest.approx(list(k), rel=1e-9)
        assert list(result["d"]) == pytest.approx(list(d), rel=1e-9)

    def test_j_is_3k_minus_2d(self):
        df = frame(80)
        result = kdj(df["high"], df["low"], df["close"])
        assert list(result["j"]) == pytest.approx(list(3.0 * result["k"] - 2.0 * result["d"]))

    def test_flat_window_is_neutral(self):
        high = low = close = pd.Series([10.0] * 30)
        result = kdj(high, low, close)
        assert result["k"].iloc[-1] == pytest.approx(50.0)
        assert result["d"].iloc[-1] == pytest.approx(50.0)

    def test_rising_series_pushes_k_up(self):
        close = pd.Series(np.arange(1.0, 60.0))
        result = kdj(close, close, close)
        assert result["k"].iloc[-1] > 50


class TestBoll:
    def test_uses_population_std(self):
        """ddof=0 parity check against a direct computation."""
        close = frame(100, seed=2)["close"]
        result = boll(close, 20, 2.0)
        mid = close.rolling(20, min_periods=20).mean()
        std = close.rolling(20, min_periods=20).std(ddof=0)
        assert list(result["mid"]) == pytest.approx(list(mid), nan_ok=True)
        assert list(result["up"]) == pytest.approx(list(mid + 2 * std), nan_ok=True)
        assert list(result["low"]) == pytest.approx(list(mid - 2 * std), nan_ok=True)

    def test_bands_are_symmetric_around_mid(self):
        result = boll(frame(80)["close"]).dropna()
        assert list(result["up"] - result["mid"]) == pytest.approx(list(result["mid"] - result["low"]))

    def test_warmup_is_nan(self):
        result = boll(frame(30)["close"], 20).dropna()
        # 30 observations yield only 11 fully-formed band values.
        assert len(result) == 11

    def test_constant_series_has_zero_width(self):
        close = pd.Series([7.0] * 30)
        result = boll(close, 20).dropna()
        assert list(result["up"]) == pytest.approx([7.0] * len(result))
        assert list(result["low"]) == pytest.approx([7.0] * len(result))
        assert list(result["up"] - result["low"]) == pytest.approx([0.0] * len(result))


class TestAtr:
    def test_true_range_includes_gap(self):
        high = pd.Series([10.0, 12.0])
        low = pd.Series([9.0, 11.0])
        close = pd.Series([9.5, 11.5])
        # Second bar gaps up from close 9.5: |12 - 9.5| = 2.5 beats 1.0.
        result = true_range(high, low, close)
        assert result.iloc[1] == pytest.approx(2.5)

    def test_atr_of_constant_range_equals_that_range(self):
        high = pd.Series([11.0] * 40)
        low = pd.Series([10.0] * 40)
        close = pd.Series([10.5] * 40)
        result = atr(high, low, close, 14)
        assert result.iloc[-1] == pytest.approx(1.0)

    def test_atr_uses_wilder_smoothing(self):
        df = frame(150)
        result = atr(df["high"], df["low"], df["close"], 14)
        expected = wilder(true_range(df["high"], df["low"], df["close"]), 14)
        assert list(result) == pytest.approx(list(expected), nan_ok=True)


class TestObv:
    def test_adds_on_up_days_subtracts_on_down_days(self):
        close = pd.Series([10.0, 11.0, 12.0, 11.0, 11.0])
        volume = pd.Series([100.0, 200.0, 300.0, 400.0, 500.0])
        result = obv(close, volume)
        assert list(result) == pytest.approx([0.0, 200.0, 500.0, 100.0, 100.0])

    def test_flat_close_leaves_obv_unchanged(self):
        close = pd.Series([5.0] * 4)
        volume = pd.Series([1.0, 2.0, 3.0, 4.0])
        assert list(obv(close, volume)) == pytest.approx([0.0] * 4)


class TestVolumeRatio:
    def test_ratio_against_preceding_mean(self):
        volume = pd.Series([100.0, 100.0, 100.0, 100.0, 100.0, 200.0])
        result = volume_ratio(volume, 5)
        assert result.iloc[5] == pytest.approx(2.0)

    def test_first_values_are_nan(self):
        result = volume_ratio(pd.Series([1.0] * 10), 5)
        assert result.iloc[:5].isna().all()

    def test_current_day_excluded_from_baseline(self):
        """A volume spike must not dilute its own baseline."""
        volume = pd.Series([10.0] * 5 + [1000.0])
        result = volume_ratio(volume, 5)
        assert result.iloc[5] == pytest.approx(100.0)

    def test_zero_baseline_is_nan_not_inf(self):
        volume = pd.Series([0.0] * 6)
        result = volume_ratio(volume, 5)
        assert result.iloc[5:].isna().all()


class TestComputeIndicators:
    def test_returns_all_declared_columns(self):
        result = compute_indicators(frame(300))
        assert list(result.columns) == list(INDICATOR_COLUMNS)

    def test_index_is_preserved(self):
        df = frame(300)
        df.index = pd.date_range("2024-01-01", periods=len(df), freq="B")
        result = compute_indicators(df)
        assert result.index.equals(df.index)

    def test_empty_frame_returns_empty_with_columns(self):
        result = compute_indicators(pd.DataFrame())
        assert result.empty
        assert list(result.columns) == list(INDICATOR_COLUMNS)

    def test_atr_pct_is_percent_of_close(self):
        df = frame(200)
        result = compute_indicators(df)
        expected = result["atr14"] / df["close"] * 100.0
        assert list(result["atr_pct"]) == pytest.approx(list(expected), nan_ok=True)

    def test_missing_volume_column_is_tolerated(self):
        df = frame(200).drop(columns=["volume"])
        result = compute_indicators(df)
        assert list(result.columns) == list(INDICATOR_COLUMNS)

    def test_no_future_leakage_across_truncation(self):
        """Every indicator at t must be identical when later rows are removed.

        This is the single most important property for backtesting: if any
        indicator depended on subsequent bars, results would be optimistic.
        """
        full_df = frame(260, seed=5)
        cut = 200
        full = compute_indicators(full_df)
        partial = compute_indicators(full_df.iloc[:cut])

        for column in INDICATOR_COLUMNS:
            a = full[column].iloc[:cut]
            b = partial[column]
            mask = a.notna() & b.notna()
            assert mask.sum() > 0, f"{column} produced no overlapping values"
            np.testing.assert_allclose(
                a[mask].to_numpy(),
                b[mask].to_numpy(),
                rtol=1e-9,
                atol=1e-9,
                err_msg=f"{column} leaks future information",
            )

    def test_single_row_does_not_crash(self):
        result = compute_indicators(frame(1))
        assert len(result) == 1

    def test_values_are_finite_or_nan(self):
        result = compute_indicators(frame(120))
        values = result.to_numpy(dtype=float)
        assert not np.isinf(values).any()
