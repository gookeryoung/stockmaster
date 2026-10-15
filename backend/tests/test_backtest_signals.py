"""Score-history tests for backtesting.

These guard the property that matters for a credible score backtest: scores must
be produced **per date**, cross-sectionally standardised within each date, and
must not leak future information into earlier scores.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.backtest.signals import (
    build_score_history,
    factor_frame_for_history,
    summarize_scores,
    top_n_per_date,
)


def bars(n: int = 300, seed: int = 1, drift: float = 0.001, start: date = date(2024, 1, 1)):
    rng = np.random.default_rng(seed)
    dates: list[date] = []
    day = start
    while len(dates) < n:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    close = np.maximum(50.0 + np.cumsum(rng.normal(drift * 50, 1.0, n)), 1.0)
    volume = np.abs(rng.normal(1e6, 1e5, n))
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "turn": np.abs(rng.normal(0.01, 0.002, n)),
        }
    )


def universe(count: int = 5, n: int = 300) -> dict[str, pd.DataFrame]:
    return {f"60000{i}.SH": bars(n=n, seed=i + 1, drift=0.0005 * (i + 1)) for i in range(count)}


class TestFactorFrameForHistory:
    def test_returns_long_frame_with_code_and_date(self):
        frame = factor_frame_for_history(universe(3))
        assert not frame.empty
        assert {"trade_date", "code"}.issubset(frame.columns)
        assert frame["code"].nunique() == 3

    def test_short_history_is_skipped(self):
        """Symbols without enough bars for the longest window cannot score."""
        frame = factor_frame_for_history({"600000.SH": bars(n=40)})
        assert frame.empty

    def test_empty_input(self):
        assert factor_frame_for_history({}).empty

    def test_no_future_leakage(self):
        """Truncating the future must not change an earlier date's factors."""
        data = universe(3, n=260)
        full = factor_frame_for_history(data)

        truncated = {}
        for code, frame in data.items():
            truncated[code] = frame.iloc[:200].copy()
        partial = factor_frame_for_history(truncated)

        cutoff = truncated["600000.SH"]["trade_date"].iloc[-1]
        full_early = full[full["trade_date"] <= cutoff]
        partial_last = partial[partial["trade_date"] == cutoff]

        for column in ("mom_20", "mom_60", "vol_20", "rsi_14"):
            a = full_early[full_early["trade_date"] == cutoff].sort_values("code")[column].reset_index(drop=True)
            b = partial_last.sort_values("code")[column].reset_index(drop=True)
            mask = a.notna() & b.notna()
            if mask.sum() == 0:
                continue
            np.testing.assert_allclose(
                a[mask].to_numpy(),
                b[mask].to_numpy(),
                rtol=1e-9,
                err_msg=f"{column} leaks future information",
            )


class TestBuildScoreHistory:
    def test_produces_multiple_dates(self):
        """The core requirement: more than one rebalance point is possible."""
        history = build_score_history(universe(5))
        assert not history.empty
        assert history["trade_date"].nunique() > 20

    def test_score_is_per_date_standardised(self):
        history = build_score_history(universe(5))
        for _day, group in history.groupby("trade_date"):
            if len(group) < 2:
                continue
            # The composite of z-scores is centred near zero by construction.
            assert abs(group["score"].mean()) < 1.0

    def test_single_symbol_yields_no_history(self):
        """A one-name cross-section has no dispersion, so no meaningful score."""
        history = build_score_history({"600000.SH": bars(n=300)})
        assert history.empty

    def test_empty_input(self):
        assert build_score_history({}).empty

    def test_columns_are_stable(self):
        history = build_score_history(universe(3))
        assert list(history.columns) == ["trade_date", "code", "score"]

    def test_sorted_by_date_then_code(self):
        history = build_score_history(universe(4))
        keys = list(zip(history["trade_date"], history["code"], strict=False))
        assert keys == sorted(keys)


class TestTopNPerDate:
    def test_returns_one_portfolio_per_date(self):
        history = build_score_history(universe(5))
        signals = top_n_per_date(history, 2)
        assert signals
        assert all(len(weights) <= 2 for weights in signals.values())

    def test_weights_sum_to_one(self):
        history = build_score_history(universe(5))
        signals = top_n_per_date(history, 3)
        for weights in signals.values():
            assert sum(weights.values()) == pytest.approx(1.0)

    def test_selects_highest_scores(self):
        history = build_score_history(universe(5))
        signals = top_n_per_date(history, 1)
        for day, weights in list(signals.items())[:5]:
            chosen = next(iter(weights))
            candidates = history[history["trade_date"] == day]
            assert chosen == candidates.loc[candidates["score"].idxmax(), "code"]

    def test_zero_top_n_returns_nothing(self):
        history = build_score_history(universe(3))
        assert top_n_per_date(history, 0) == {}

    def test_empty_history(self):
        assert top_n_per_date(pd.DataFrame(columns=["trade_date", "code", "score"]), 3) == {}


class TestSummarize:
    def test_reports_coverage(self):
        history = build_score_history(universe(4))
        summary = summarize_scores(history)
        assert summary["dates"] > 0
        assert summary["symbols"] == 4
        assert summary["rows"] > 0

    def test_empty_summary(self):
        assert summarize_scores(pd.DataFrame()) == {"dates": 0, "symbols": 0, "rows": 0}
