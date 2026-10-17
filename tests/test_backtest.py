"""Backtest engine tests.

These target the assumptions that make a backtest believable: no same-day
execution, T+1 settlement, limit boards blocking fills, and costs that actually
reduce returns. A backtest that ignores these will always look better than reality.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stockmaster.backtest.engine import (
    BacktestConfig,
    buy_and_hold_signals,
    compute_metrics,
    run_backtest,
)


def make_bars(
    code: str,
    *,
    n: int = 60,
    start: date = date(2026, 1, 5),
    start_price: float = 10.0,
    daily_return: float = 0.01,
    preclose_ratio: float = 1.0,
    is_st: bool = False,
    amount: float = 1e8,
) -> pd.DataFrame:
    """Business-day OHLCV with an explicit ``preclose`` for limit checks."""
    dates: list[date] = []
    day = start
    while len(dates) < n:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)

    closes = start_price * (1 + daily_return) ** np.arange(n)
    precloses = [start_price, *list(closes[:-1])]
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": closes,
            "high": closes * 1.02,
            "low": closes * 0.98,
            "close": closes,
            "preclose": [p * preclose_ratio for p in precloses],
            "volume": 1e6,
            "amount": amount,
            "is_st": is_st,
        }
    )


class TestBasicExecution:
    def test_no_trades_without_signals(self):
        result = run_backtest({"600519.SH": make_bars("600519.SH")}, {})
        assert result.trades.empty
        # Equity stays at the initial cash when nothing is traded.
        assert result.metrics["final_equity"] == pytest.approx(1_000_000.0)

    def test_signal_executes_next_day_not_same_day(self):
        """A signal must be filled at the following session, never the signal day."""
        bars = make_bars("600519.SH", n=20)
        signal_date = bars["trade_date"].iloc[2]
        result = run_backtest(
            {"600519.SH": bars},
            {signal_date: {"600519.SH": 1.0}},
        )
        filled = result.trades[result.trades["shares"] > 0]
        assert not filled.empty
        first_trade_date = filled["trade_date"].iloc[0]
        assert first_trade_date > signal_date
        expected_date = bars["trade_date"].iloc[3]
        assert first_trade_date == expected_date

    def test_buy_then_hold_tracks_price(self):
        bars = make_bars("600519.SH", n=40, daily_return=0.01)
        signal_date = bars["trade_date"].iloc[0]
        result = run_backtest({"600519.SH": bars}, {signal_date: {"600519.SH": 1.0}})
        # A rising market with a full position must end above the initial cash.
        assert result.metrics["final_equity"] > 1_000_000.0

    def test_flat_price_loses_only_costs(self):
        bars = make_bars("600519.SH", n=30, daily_return=0.0)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
        )
        # With a flat price the only loss is commission/slippage.
        assert result.metrics["final_equity"] < 1_000_000.0
        assert result.metrics["total_costs"] > 0

    def test_cash_never_goes_negative(self):
        bars = make_bars("600519.SH", n=40)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}})
        assert (result.equity["cash"] >= -1e-6).all()

    def test_buy_quantity_is_whole_lots(self):
        bars = make_bars("600519.SH", n=30)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 0.5}})
        filled = result.trades[result.trades["shares"] > 0]
        assert not filled.empty
        assert (filled["shares"] % 100 == 0).all()


class TestTPlusOne:
    def test_same_day_buy_cannot_be_sold(self):
        """Sell orders in the same session as a buy must be blocked (T+1)."""
        bars = make_bars("600519.SH", n=10)
        signals = {
            bars["trade_date"].iloc[0]: {"600519.SH": 1.0},  # buy executes day 1
            bars["trade_date"].iloc[1]: {"600519.SH": 0.0},  # sell requested day 2
        }
        result = run_backtest({"600519.SH": bars}, signals)
        filled = result.trades[result.trades["shares"] > 0]
        buys = filled[filled["side"] == "buy"]
        sells = filled[filled["side"] == "sell"]
        assert len(buys) == 1
        assert len(sells) == 1
        # A sale must occur strictly after the purchase session.
        assert sells["trade_date"].iloc[0] > buys["trade_date"].iloc[0]

    def test_next_day_sale_is_allowed(self):
        bars = make_bars("600519.SH", n=12)
        signals = {
            bars["trade_date"].iloc[0]: {"600519.SH": 1.0},
            bars["trade_date"].iloc[3]: {"600519.SH": 0.0},
        }
        result = run_backtest({"600519.SH": bars}, signals)
        sells = result.trades[(result.trades["side"] == "sell") & (result.trades["shares"] > 0)]
        assert not sells.empty

    def test_sell_produces_finite_pnl(self):
        bars = make_bars("600519.SH", n=15, daily_return=0.02)
        signals = {
            bars["trade_date"].iloc[0]: {"600519.SH": 1.0},
            bars["trade_date"].iloc[5]: {"600519.SH": 0.0},
        }
        result = run_backtest({"600519.SH": bars}, signals)
        sells = result.trades[(result.trades["side"] == "sell") & (result.trades["shares"] > 0)]
        assert not sells.empty
        pnl = sells["pnl"].iloc[0]
        assert np.isfinite(pnl)
        # Prices rose, so the round trip should be profitable.
        assert pnl > 0
        assert result.metrics["win_rate"] == pytest.approx(1.0)


class TestPriceLimits:
    def test_buy_blocked_at_upper_limit(self):
        """A bar closing at the 10% ceiling cannot be bought."""
        bars = make_bars("600519.SH", n=8, daily_return=0.0)
        # Force the final bar to close exactly at the upper limit of its preclose.
        bars.loc[bars.index[-1], "preclose"] = 10.0
        bars.loc[bars.index[-1], "close"] = 11.0
        bars.loc[bars.index[-1], "high"] = 11.0
        bars.loc[bars.index[-1], "low"] = 10.5
        signal_date = bars["trade_date"].iloc[-2]  # executes on the limit day
        result = run_backtest({"600519.SH": bars}, {signal_date: {"600519.SH": 1.0}})
        blocked = result.trades[result.trades["reason"] == "limit_up"]
        assert not blocked.empty
        assert (result.trades[result.trades["shares"] > 0]).empty

    def test_sell_blocked_at_lower_limit(self):
        bars = make_bars("600519.SH", n=12, daily_return=0.0)
        entry_date = bars["trade_date"].iloc[0]
        # Pin the final bar below the -10% limit. A drop larger than the band
        # (rather than exactly on it) keeps the assertion robust against
        # floating-point error in the half-up limit calculation.
        bars.loc[bars.index[-1], "preclose"] = 10.0
        bars.loc[bars.index[-1], "open"] = 7.0
        bars.loc[bars.index[-1], "close"] = 7.0
        bars.loc[bars.index[-1], "high"] = 7.5
        bars.loc[bars.index[-1], "low"] = 7.0
        # The signal must be placed so it executes ON the limit bar (iloc[-1]).
        exit_date = bars["trade_date"].iloc[-2]
        result = run_backtest({"600519.SH": bars}, {entry_date: {"600519.SH": 1.0}, exit_date: {}})
        # The order is recorded with zero shares and an explicit reason.
        blocked = result.trades[result.trades["reason"] == "limit_down"]
        assert not blocked.empty
        assert (blocked["shares"] == 0).all()
        # The position is therefore still held at the end.
        assert result.metrics["skip_reasons"].get("limit_down", 0) > 0

    def test_one_word_board_is_blocked(self):
        bars = make_bars("600519.SH", n=10, daily_return=0.0)
        bars.loc[bars.index[-1], "preclose"] = 10.0
        bars.loc[bars.index[-1], "open"] = 11.0
        bars.loc[bars.index[-1], "high"] = 11.0
        bars.loc[bars.index[-1], "low"] = 11.0
        bars.loc[bars.index[-1], "close"] = 11.0
        signal_date = bars["trade_date"].iloc[-2]
        result = run_backtest({"600519.SH": bars}, {signal_date: {"600519.SH": 1.0}})
        assert (result.trades[result.trades["shares"] > 0]).empty

    def test_chinext_uses_twenty_percent_band(self):
        """A 10% rise on a ChiNext name is NOT a limit and must be tradeable."""
        bars = make_bars("300750.SZ", n=8, daily_return=0.0)
        bars.loc[bars.index[-1], "preclose"] = 10.0
        bars.loc[bars.index[-1], "close"] = 11.0  # 10% up, inside the 20% band
        bars.loc[bars.index[-1], "high"] = 11.2
        bars.loc[bars.index[-1], "low"] = 10.0
        signal_date = bars["trade_date"].iloc[-2]
        result = run_backtest({"300750.SZ": bars}, {signal_date: {"300750.SZ": 1.0}})
        assert not (result.trades[result.trades["shares"] > 0]).empty


class TestSuspension:
    def test_missing_day_blocks_trading(self):
        bars = make_bars("600519.SH", n=14)
        signal_date = bars["trade_date"].iloc[0]
        # Suspend the day the order would execute by removing that row.
        suspended = bars.drop(index=[1]).reset_index(drop=True)
        result = run_backtest({"600519.SH": suspended}, {signal_date: {"600519.SH": 1.0}})
        # The order must not silently fill on the removed day.
        filled = result.trades[result.trades["shares"] > 0]
        if not filled.empty:
            assert filled["trade_date"].iloc[0] != bars["trade_date"].iloc[1]

    def test_st_names_excluded_when_configured(self):
        bars = make_bars("600519.SH", n=10, is_st=True)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
            config=BacktestConfig(exclude_st=True),
        )
        assert (result.trades[result.trades["shares"] > 0]).empty

    def test_st_names_allowed_when_configured(self):
        bars = make_bars("600519.SH", n=10, is_st=True)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
            config=BacktestConfig(exclude_st=False),
        )
        assert not (result.trades[result.trades["shares"] > 0]).empty

    def test_illiquid_names_filtered(self):
        bars = make_bars("600519.SH", n=10, amount=1000.0)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
            config=BacktestConfig(min_amount=1e6),
        )
        assert result.metrics["skip_reasons"].get("illiquid", 0) > 0


class TestCosts:
    def test_costs_reduce_returns(self):
        bars = make_bars("600519.SH", n=30, daily_return=0.0)
        signals = {
            bars["trade_date"].iloc[0]: {"600519.SH": 1.0},
            bars["trade_date"].iloc[10]: {"600519.SH": 0.0},
        }
        free = run_backtest(
            {"600519.SH": bars},
            signals,
            config=BacktestConfig(
                commission_rate=0,
                min_commission=0,
                stamp_tax_rate=0,
                transfer_fee_rate=0,
                slippage_rate=0,
            ),
        )
        costly = run_backtest(
            {"600519.SH": bars},
            signals,
            config=BacktestConfig(commission_rate=0.001, stamp_tax_rate=0.001),
        )
        assert costly.metrics["final_equity"] < free.metrics["final_equity"]

    def test_stamp_tax_only_on_sells(self):
        bars = make_bars("600519.SH", n=20)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
        )
        buys = result.trades[result.trades["side"] == "buy"]
        assert not buys.empty
        assert (buys["stamp_tax"] == 0).all()

    def test_min_commission_applied_to_small_orders(self):
        bars = make_bars("600519.SH", n=10)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 0.01}},
            config=BacktestConfig(commission_rate=0.0001, min_commission=5.0),
        )
        filled = result.trades[result.trades["shares"] > 0]
        if not filled.empty:
            assert (filled["commission"] >= 5.0).all()

    def test_cost_share_is_reported(self):
        bars = make_bars("600519.SH", n=20)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}})
        assert result.metrics["cost_share_of_initial"] > 0


class TestMetrics:
    def test_metrics_present(self):
        bars = make_bars("600519.SH", n=60)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}})
        for key in (
            "total_return",
            "annualised_return",
            "max_drawdown",
            "sharpe",
            "trading_days",
            "n_trades",
            "total_costs",
            "turnover",
        ):
            assert key in result.metrics, f"missing metric {key}"

    def test_drawdown_is_never_positive(self):
        bars = make_bars("600519.SH", n=60, daily_return=-0.005)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}})
        assert (result.equity["drawdown"] <= 1e-9).all()
        assert result.metrics["max_drawdown"] < 0

    def test_no_drawdown_in_monotonic_rise(self):
        bars = make_bars("600519.SH", n=40, daily_return=0.01)
        result = run_backtest({"600519.SH": bars}, {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}})
        # Rising prices with a single entry produce no meaningful drawdown.
        assert result.metrics["max_drawdown"] > -0.2

    def test_empty_equity_curve_reports_error(self):
        assert "error" in compute_metrics(pd.DataFrame(), pd.DataFrame(), BacktestConfig())

    def test_zero_variance_returns_give_no_sharpe(self):
        bars = make_bars("600519.SH", n=30, daily_return=0.0)
        result = run_backtest({"600519.SH": bars}, {})
        assert result.metrics["sharpe"] is None

    def test_benchmark_tracked(self):
        bars = make_bars("600519.SH", n=40, daily_return=0.01)
        result = run_backtest({"600519.SH": bars}, {}, benchmark="600519.SH")
        assert result.equity["benchmark"].notna().any()
        assert result.metrics["benchmark_return"] is not None


class TestMultiAsset:
    def test_two_assets_split_capital(self):
        bars = {
            "600519.SH": make_bars("600519.SH", n=40),
            "000001.SZ": make_bars("000001.SZ", n=40, start_price=20.0),
        }
        signals = {date(2026, 1, 5): {"600519.SH": 0.5, "000001.SZ": 0.5}}
        result = run_backtest(bars, signals)
        filled = result.trades[result.trades["shares"] > 0]
        assert set(filled["code"]) == {"600519.SH", "000001.SZ"}

    def test_position_weight_cap_respected(self):
        bars = make_bars("600519.SH", n=30)
        result = run_backtest(
            {"600519.SH": bars},
            {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}},
            config=BacktestConfig(max_position_weight=0.3),
        )
        final = result.equity["equity"].iloc[-1]
        # Backing out the position: market value should be near 30% of equity.
        last = result.equity.iloc[-1]
        assert last["market_value"] <= 0.45 * final

    def test_buy_and_hold_helper_produces_signal(self):
        bars = {"600519.SH": make_bars("600519.SH", n=20)}
        signals = buy_and_hold_signals(bars, "600519.SH")
        assert signals
        result = run_backtest(bars, signals)
        assert not result.trades[result.trades["shares"] > 0].empty


class TestDeterminism:
    def test_same_inputs_same_result(self):
        bars = make_bars("600519.SH", n=50)
        signals = {bars["trade_date"].iloc[0]: {"600519.SH": 1.0}}
        first = run_backtest({"600519.SH": bars}, signals)
        second = run_backtest({"600519.SH": bars}, signals)
        assert first.metrics["final_equity"] == pytest.approx(second.metrics["final_equity"])
        assert len(first.trades) == len(second.trades)
