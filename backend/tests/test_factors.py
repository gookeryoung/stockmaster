"""Factor engine tests.

Two properties matter most:
1. Factors must be numerically correct and point-in-time.
2. Cross-sectional transforms must be computed *within a date*, never pooled
   across dates — pooling would leak future information (relative rank versus
   names that had not yet traded) and is the usual way factor pipelines go wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.factors.compute import (
    DEFAULT_SCORE_WEIGHTS,
    PCT_COLUMNS,
    RAW_FACTOR_COLUMNS,
    ZSCORE_COLUMNS,
    composite_score,
    compute_factors,
    screen_frame,
    standardize_cross_section,
)


def frame(n: int = 300, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = np.maximum(100.0 + np.cumsum(rng.normal(0.05, 1.0, n)), 1.0)
    high = close * 1.01
    low = close * 0.99
    volume = np.abs(rng.normal(1e6, 1e5, n))
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "turn": np.abs(rng.normal(0.01, 0.002, n)),
        }
    )


class TestComputeFactors:
    def test_returns_declared_columns(self):
        result = compute_factors(frame())
        assert list(result.columns) == list(RAW_FACTOR_COLUMNS)

    def test_empty_frame(self):
        result = compute_factors(pd.DataFrame())
        assert result.empty
        assert list(result.columns) == list(RAW_FACTOR_COLUMNS)

    def test_momentum_matches_definition(self):
        df = frame(200)
        result = compute_factors(df)
        expected = (df["close"] / df["close"].shift(20) - 1.0) * 100.0
        assert list(result["mom_20"]) == pytest.approx(list(expected), nan_ok=True)

    def test_momentum_of_flat_series_is_zero(self):
        df = frame(120)
        df["close"] = 50.0
        df["high"] = 50.0
        df["low"] = 50.0
        result = compute_factors(df)
        assert result["mom_20"].iloc[-1] == pytest.approx(0.0)

    def test_volatility_is_zero_for_flat_series(self):
        df = frame(120)
        df["close"] = 50.0
        df["high"] = 50.0
        df["low"] = 50.0
        result = compute_factors(df)
        assert result["vol_20"].iloc[-1] == pytest.approx(0.0)

    def test_volatility_is_annualised(self):
        df = frame(200)
        result = compute_factors(df)
        daily = df["close"].pct_change().rolling(20, min_periods=20).std(ddof=0)
        expected = daily * np.sqrt(252)
        assert list(result["vol_20"]) == pytest.approx(list(expected), nan_ok=True)

    def test_dist_high_is_zero_at_new_high(self):
        df = frame(300)
        # Drive a monotonic rise so the last bar is the 252-day high.
        df["close"] = np.arange(1.0, 301.0)
        df["high"] = df["close"]
        df["low"] = df["close"]
        result = compute_factors(df)
        assert result["dist_high_252"].iloc[-1] == pytest.approx(0.0)

    def test_dist_high_is_negative_below_high(self):
        df = frame(300)
        df["close"] = 100.0
        df.loc[df.index[-1], "close"] = 50.0
        df["high"] = df["close"].cummax()
        df["low"] = df["close"]
        result = compute_factors(df)
        assert result["dist_high_252"].iloc[-1] < 0

    def test_breakout_uses_prior_high_only(self):
        """A bar must not count itself as its own breakout reference."""
        df = frame(60)
        df["close"] = 10.0
        df["high"] = 10.0
        df["low"] = 10.0
        # Last bar makes a clear new high above all prior bars.
        df.loc[df.index[-1], "close"] = 20.0
        df.loc[df.index[-1], "high"] = 20.0
        result = compute_factors(df)
        assert result["breakout_20"].iloc[-1] == pytest.approx(1.0)

        # An unchanged bar at the equal-high level must not signal breakout.
        df2 = frame(60)
        df2["close"] = 10.0
        df2["high"] = 10.0
        df2["low"] = 10.0
        result2 = compute_factors(df2)
        assert result2["breakout_20"].iloc[-1] == pytest.approx(0.0)

    def test_ma_bull_reflects_ordering(self):
        df = frame(200)
        df["close"] = np.arange(1.0, 201.0)  # steady uptrend
        df["high"] = df["close"]
        df["low"] = df["close"]
        result = compute_factors(df)
        assert result["ma_bull"].iloc[-1] == pytest.approx(1.0)

        df2 = frame(200)
        df2["close"] = np.arange(200.0, 0.0, -1.0)  # steady downtrend
        df2["high"] = df2["close"]
        df2["low"] = df2["close"]
        assert compute_factors(df2)["ma_bull"].iloc[-1] == pytest.approx(0.0)

    def test_rsi_is_bounded(self):
        result = compute_factors(frame(300))["rsi_14"].dropna()
        assert ((result >= 0) & (result <= 100)).all()

    def test_missing_optional_columns_are_tolerated(self):
        df = frame(200).drop(columns=["amount", "turn"])
        result = compute_factors(df)
        assert list(result.columns) == list(RAW_FACTOR_COLUMNS)
        assert result["turnover_20"].isna().all()
        assert result["amount_log_20"].isna().all()

    def test_amihud_positive(self):
        result = compute_factors(frame(200))["amihud_20"].dropna()
        assert (result >= 0).all()

    def test_no_future_leakage(self):
        """Truncating the future must not change any past factor value."""
        full_df = frame(320, seed=9)
        cut = 260
        full = compute_factors(full_df)
        partial = compute_factors(full_df.iloc[:cut])
        for column in RAW_FACTOR_COLUMNS:
            a = full[column].iloc[:cut]
            b = partial[column]
            mask = a.notna() & b.notna()
            assert mask.sum() > 0, f"{column} had no overlapping non-null values"
            np.testing.assert_allclose(
                a[mask].to_numpy(),
                b[mask].to_numpy(),
                rtol=1e-9,
                atol=1e-9,
                err_msg=f"{column} leaks future information",
            )


def cross_section() -> pd.DataFrame:
    """Two dates, three names each, with deliberately different moments."""
    rows = []
    for day, offset in (("2026-01-05", 0.0), ("2026-01-06", 100.0)):
        for i, code in enumerate(("600519.SH", "000001.SZ", "300750.SZ")):
            rows.append(
                {
                    "trade_date": day,
                    "code": code,
                    "mom_20": offset + i * 3.0,
                    "mom_60": offset + i * 5.0,
                    "vol_20": 0.2 + i * 0.1,
                    "turnover_20": 0.01 + i * 0.01,
                    "rsi_14": 40.0 + i * 10.0,
                }
            )
    return pd.DataFrame(rows)


class TestStandardize:
    def test_adds_expected_columns(self):
        result = standardize_cross_section(cross_section())
        for column in (*ZSCORE_COLUMNS, *PCT_COLUMNS):
            assert column in result.columns

    def test_zscores_are_per_date(self):
        result = standardize_cross_section(cross_section())
        for _day, group in result.groupby("trade_date"):
            assert group["z_mom_20"].mean() == pytest.approx(0.0, abs=1e-9)
            assert group["z_mom_20"].std(ddof=0) == pytest.approx(1.0, abs=1e-9)

    def test_zscores_differ_between_dates_with_different_levels(self):
        """Pooling dates would collapse both dates onto one mean of zero.

        Proper per-date standardisation gives each date its own identical shape,
        so the two dates' z-score *vectors* should match even though their raw
        momentum levels differ by 100.
        """
        result = standardize_cross_section(cross_section())
        first = result[result["trade_date"] == "2026-01-05"]["z_mom_20"].tolist()
        second = result[result["trade_date"] == "2026-01-06"]["z_mom_20"].tolist()
        assert first == pytest.approx(second)
        # And the pooled z-score would NOT match the per-date ones.
        pooled = (result["mom_20"] - result["mom_20"].mean()) / result["mom_20"].std(ddof=0)
        assert pooled.iloc[0] != pytest.approx(result["z_mom_20"].iloc[0])

    def test_percentile_rank_is_within_date(self):
        result = standardize_cross_section(cross_section())
        for _day, group in result.groupby("trade_date"):
            ranks = group.sort_values("mom_20")["pct_mom_20"].tolist()
            # Ascending raw momentum -> ascending percentile rank.
            assert ranks == sorted(ranks)
            assert min(ranks) == pytest.approx(1 / 3)
            assert max(ranks) == pytest.approx(1.0)

    def test_single_name_date_yields_nan_zscores(self):
        """One observation has no dispersion; z-scoring it would be meaningless."""
        frame_one = cross_section()
        single = frame_one[frame_one["trade_date"] == "2026-01-05"].head(1)
        result = standardize_cross_section(single)
        assert result["z_mom_20"].isna().all()

    def test_constant_factor_yields_nan_not_inf(self):
        frame_const = cross_section()
        frame_const["vol_20"] = 0.3
        result = standardize_cross_section(frame_const)
        assert result["z_vol_20"].isna().all()

    def test_empty_frame(self):
        result = standardize_cross_section(pd.DataFrame())
        assert result.empty

    def test_missing_source_column_produces_nan(self):
        frame_missing = cross_section().drop(columns=["rsi_14"])
        result = standardize_cross_section(frame_missing)
        assert result["z_rsi_14"].isna().all()


class TestCompositeScore:
    def test_score_uses_declared_weights(self):
        result = standardize_cross_section(cross_section())
        result["score"] = composite_score(result)
        manual = sum(result[column].fillna(0.0) * weight for column, weight in DEFAULT_SCORE_WEIGHTS.items()) / sum(
            abs(w) for w in DEFAULT_SCORE_WEIGHTS.values()
        )
        assert list(result["score"]) == pytest.approx(list(manual))

    def test_high_momentum_beats_low_momentum(self):
        result = standardize_cross_section(cross_section())
        result["score"] = composite_score(result)
        group = result[result["trade_date"] == "2026-01-05"]
        best = group.loc[group["mom_20"].idxmax(), "score"]
        worst = group.loc[group["mom_20"].idxmin(), "score"]
        assert best > worst

    def test_missing_factors_renormalise_weights(self):
        """A row with only momentum available must still get a usable score."""
        frame_partial = cross_section()[["trade_date", "code", "mom_20", "mom_60"]]
        result = standardize_cross_section(frame_partial)
        result["score"] = composite_score(result)
        assert result["score"].notna().any()

    def test_row_with_no_factors_is_nan(self):
        frame_none = cross_section()[["trade_date", "code"]]
        score = composite_score(frame_none)
        assert score.isna().all()

    def test_custom_weights_respected(self):
        result = standardize_cross_section(cross_section())
        momentum_only = composite_score(result, {"z_mom_20": 1.0})
        assert list(momentum_only) == pytest.approx(list(result["z_mom_20"]))


class TestScreening:
    def build(self):
        result = standardize_cross_section(cross_section())
        result["score"] = composite_score(result)
        result["is_st"] = [False, True, False, False, False, False]
        return result

    def test_excludes_st_by_default(self):
        out = screen_frame(self.build())
        assert not out["is_st"].any()

    def test_min_score_filter(self):
        out = screen_frame(self.build(), min_score=10.0)
        assert out.empty or (out["score"] >= 10.0).all()

    def test_st_can_be_included(self):
        out = screen_frame(self.build(), exclude_st=False)
        assert out["is_st"].any()

    def test_max_vol_filter(self):
        out = screen_frame(self.build(), max_vol=0.25)
        assert (out["vol_20"] <= 0.25).all()
