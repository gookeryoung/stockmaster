"""Backtest engine for daily A-share strategies.

Realism rules that are *enforced*, not assumed
---------------------------------------------
* **No lookahead**: a signal derived from day ``t`` is executed at day ``t+1``'s
  open. Signals are shifted explicitly rather than relying on caller discipline.
* **T+1 settlement**: shares bought on ``t+1`` cannot be sold until ``t+2``.
* **Price limits**: buys are blocked at the upper limit and sells at the lower
  limit, because those orders cannot realistically fill. Computed from
  ``preclose`` with board-aware ratios.
* **Suspension**: a symbol with no bar on a day cannot be traded that day.
* **Costs**: commission with a per-order minimum, sell-side stamp duty, transfer
  fee, and slippage in basis points.

Weights are target portfolio weights produced by a strategy callable. The engine
handles the position drift, lot rounding, cash accounting and trade ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from stockmaster.core.logging import get_logger
from stockmaster.core.market import LOT_SIZE, is_limit_down, is_limit_up

log = get_logger(__name__)

__all__ = ["BacktestConfig", "BacktestResult", "buy_and_hold_signals", "run_backtest"]


@dataclass
class BacktestConfig:
    """Execution assumptions. Defaults reflect typical A-share retail costs."""

    initial_cash: float = 1_000_000.0
    #: Commission as a fraction of traded value, with a per-order minimum.
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    #: Stamp duty on the sell side only.
    stamp_tax_rate: float = 0.0005
    #: Transfer fee (charged both sides on Shanghai/Beijing historically).
    transfer_fee_rate: float = 0.00001
    #: Slippage applied against the trader, as a fraction of price.
    slippage_rate: float = 0.0005
    #: Refuse to trade names whose 20-day average amount is below this (yuan).
    min_amount: float = 0.0
    #: Skip ST names entirely when True.
    exclude_st: bool = True
    #: Maximum share of equity in any single name.
    max_position_weight: float = 1.0


@dataclass
class BacktestResult:
    """Outputs of a backtest run."""

    equity: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict
    config: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "metrics": self.metrics,
            "config": self.config,
            "warnings": self.warnings,
            "equity_points": len(self.equity),
            "trades": len(self.trades),
        }


def _costs(side: str, gross: float, config: BacktestConfig) -> dict:
    """Transaction costs for one order (all in yuan)."""
    commission = max(gross * config.commission_rate, config.min_commission)
    stamp = gross * config.stamp_tax_rate if side == "sell" else 0.0
    transfer = gross * config.transfer_fee_rate
    return {
        "commission": commission,
        "stamp_tax": stamp,
        "transfer_fee": transfer,
        "total": commission + stamp + transfer,
    }


def _prepare_prices(bars_by_symbol: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Index each symbol's frame by date for fast lookup, sorted ascending."""
    prepared: dict[str, pd.DataFrame] = {}
    for code, frame in bars_by_symbol.items():
        if frame is None or frame.empty:
            continue
        df = frame.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
        prepared[code] = df.set_index("trade_date", drop=False)
    return prepared


def _build_calendar(prices: dict[str, pd.DataFrame]) -> list[date]:
    """Union of all symbols' trading dates, ascending."""
    days: set[date] = set()
    for frame in prices.values():
        days.update(frame.index.tolist())
    return sorted(days)


def _bar(prices: dict[str, pd.DataFrame], code: str, day: date):
    frame = prices.get(code)
    if frame is None or day not in frame.index:
        return None
    return frame.loc[day]


def _is_tradeable(
    prices: dict[str, pd.DataFrame],
    code: str,
    day: date,
    side: str,
    config: BacktestConfig,
) -> tuple[bool, str]:
    """Whether an order can plausibly fill, with the reason when it cannot."""
    bar = _bar(prices, code, day)
    if bar is None:
        return False, "suspended"

    if config.exclude_st and bool(bar.get("is_st", False)):
        return False, "st"

    if config.min_amount and float(bar.get("amount", 0.0) or 0.0) < config.min_amount:
        return False, "illiquid"

    preclose = bar.get("preclose")
    close = float(bar["close"])
    if preclose is None or not np.isfinite(preclose) or preclose <= 0:
        # Without a reference price the limit cannot be verified; allow the trade
        # rather than silently dropping every order for that symbol.
        return True, ""

    high = float(bar["high"])
    low = float(bar["low"])
    is_st = bool(bar.get("is_st", False))

    if side == "buy" and is_limit_up(close, float(preclose), code, is_st=is_st):
        # A close at the ceiling means buyers could not get filled.
        return False, "limit_up"
    if side == "sell" and is_limit_down(close, float(preclose), code, is_st=is_st):
        return False, "limit_down"
    # A one-word board is untradeable in both directions.
    if high == low and (
        is_limit_up(high, float(preclose), code, is_st=is_st) or is_limit_down(low, float(preclose), code, is_st=is_st)
    ):
        return False, "one_word_limit"
    return True, ""


def run_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    signals: dict[date, dict[str, float]] | None,
    *,
    config: BacktestConfig | None = None,
    start: date | None = None,
    end: date | None = None,
    benchmark: str | None = None,
) -> BacktestResult:
    """Run a daily, weight-targeting backtest.

    Args:
        bars_by_symbol: canonical code -> ascending OHLCV frame. Frames should
            carry ``preclose`` and ``is_st`` for limit/ST enforcement.
        signals: ``{signal_date: {code: target_weight}}``. The weight is applied
            at the **next** trading day's open, so passing a signal computed from
            data up to ``signal_date`` is safe.
        config: execution assumptions.
        start/end: optional date bounds.
        benchmark: optional code to track as a buy-and-hold reference.

    Returns:
        :class:`BacktestResult` with equity curve, trade ledger and metrics.
    """
    config = config or BacktestConfig()
    prices = _prepare_prices(bars_by_symbol)
    if not prices:
        empty = pd.DataFrame(columns=["trade_date", "equity", "cash", "market_value", "drawdown"])
        return BacktestResult(
            equity=empty,
            trades=pd.DataFrame(),
            metrics={"error": "no price data"},
            config=config.__dict__.copy(),
        )

    warnings: list[str] = []
    calendar = _build_calendar(prices)
    if start is not None:
        calendar = [d for d in calendar if d >= start]
    if end is not None:
        calendar = [d for d in calendar if d <= end]
    if not calendar:
        return BacktestResult(
            equity=pd.DataFrame(),
            trades=pd.DataFrame(),
            metrics={"error": "no trading days in range"},
            config=config.__dict__.copy(),
        )

    signals = signals or {}
    signal_dates = sorted(signals)

    cash = float(config.initial_cash)
    positions: dict[str, int] = {}
    #: Average cost per share, for realised P&L on sells.
    cost_basis: dict[str, float] = {}
    #: (code, date) pairs purchased today, blocked from selling until tomorrow.
    locked: dict[str, date] = {}

    equity_rows: list[dict] = []
    trade_rows: list[dict] = []
    pending_weights: dict[str, float] | None = None
    peak = float(config.initial_cash)

    benchmark_close: list[float | None] = []
    benchmark_base: float | None = None

    # Signal at t executes at t+1, so walk the calendar carrying the last signal.
    signal_cursor = 0
    for day in calendar:
        # --- collect any signal that becomes effective today ---
        # A signal dated strictly before `day` and not yet applied is effective.
        while signal_cursor < len(signal_dates) and signal_dates[signal_cursor] < day:
            pending_weights = signals[signal_dates[signal_cursor]]
            signal_cursor += 1

        # --- mark to market at today's close ---
        market_value = 0.0
        for code, shares in positions.items():
            if shares <= 0:
                continue
            bar = _bar(prices, code, day)
            if bar is not None:
                market_value += shares * float(bar["close"])
            else:
                # Suspended: hold the last known price rather than writing to zero.
                last_known = _last_close_before(prices.get(code), day)
                market_value += shares * (last_known or 0.0)

        equity = cash + market_value

        # --- execute the pending target weights at today's open ---
        if pending_weights is not None:
            cash, positions, locked, executed = _rebalance(
                day=day,
                targets=pending_weights,
                cash=cash,
                positions=positions,
                locked=locked,
                prices=prices,
                config=config,
                equity=equity,
                cost_basis=cost_basis,
            )
            trade_rows.extend(executed)
            pending_weights = None
            # Recompute after trading: fills happen at the open, so equity moves.
            market_value = 0.0
            for code, shares in positions.items():
                if shares <= 0:
                    continue
                bar = _bar(prices, code, day)
                price = float(bar["close"]) if bar is not None else (_last_close_before(prices.get(code), day) or 0.0)
                market_value += shares * price
            equity = cash + market_value

        drawdown = (equity / peak - 1.0) if peak > 0 else 0.0
        peak = max(peak, equity)

        benchmark_value: float | None = None
        if benchmark and benchmark in prices:
            bar = _bar(prices, benchmark, day)
            if bar is not None:
                if benchmark_base is None:
                    benchmark_base = float(bar["close"])
                benchmark_value = float(bar["close"]) / benchmark_base * config.initial_cash if benchmark_base else None
        benchmark_close.append(benchmark_value)

        equity_rows.append(
            {
                "trade_date": day,
                "equity": equity,
                "cash": cash,
                "market_value": market_value,
                "drawdown": drawdown,
                "benchmark": benchmark_value,
            }
        )

    equity_frame = pd.DataFrame(equity_rows)
    trades_frame = pd.DataFrame(trade_rows)
    metrics = compute_metrics(equity_frame, trades_frame, config)
    if equity_frame.empty:
        warnings.append("no equity points produced")

    return BacktestResult(
        equity=equity_frame,
        trades=trades_frame,
        metrics=metrics,
        config={
            "initial_cash": config.initial_cash,
            "commission_rate": config.commission_rate,
            "min_commission": config.min_commission,
            "stamp_tax_rate": config.stamp_tax_rate,
            "transfer_fee_rate": config.transfer_fee_rate,
            "slippage_rate": config.slippage_rate,
            "min_amount": config.min_amount,
            "exclude_st": config.exclude_st,
            "max_position_weight": config.max_position_weight,
        },
        warnings=warnings,
    )


def _last_close_before(frame: pd.DataFrame | None, day: date) -> float | None:
    """Most recent close strictly before ``day`` (for suspended names)."""
    if frame is None:
        return None
    earlier = frame[frame.index < day]
    if earlier.empty:
        return None
    return float(earlier["close"].iloc[-1])


def _rebalance(
    *,
    day: date,
    targets: dict[str, float],
    cash: float,
    positions: dict[str, int],
    locked: dict[str, date],
    prices: dict[str, pd.DataFrame],
    config: BacktestConfig,
    equity: float,
    cost_basis: dict[str, float],
) -> tuple[float, dict[str, int], dict[str, date], list[dict]]:
    """Move the portfolio toward ``targets`` at today's open.

    Sells run first so the proceeds fund buys, which matters for a cash-constrained
    portfolio. Orders that cannot fill (limit/suspension/liquidity) are skipped and
    recorded with a reason rather than silently dropped.
    """
    executed: list[dict] = []
    positions = dict(positions)

    # Normalise and cap target weights; drop non-positive weights to zero.
    clean_targets: dict[str, float] = {}
    for code, weight in targets.items():
        if weight is None or not np.isfinite(weight) or weight <= 0:
            continue
        clean_targets[code] = min(float(weight), config.max_position_weight)

    # --- 1) sells: names held but not targeted, or overweight ---
    for code in list(positions):
        if positions.get(code, 0) <= 0:
            continue
        bar = _bar(prices, code, day)
        if bar is None:
            continue

        # T+1: shares bought today are locked.
        if locked.get(code) == day:
            continue

        current_value = positions[code] * float(bar["close"])
        target_weight = clean_targets.get(code, 0.0)
        target_value = target_weight * equity
        overshoot = current_value - target_value
        if overshoot <= 0:
            continue

        allowed, reason = _is_tradeable(prices, code, day, "sell", config)
        if not allowed:
            executed.append(_skipped_row(day, code, "sell", reason))
            continue

        price = float(bar["open"]) * (1.0 - config.slippage_rate)
        to_sell = int(np.floor(overshoot / price / LOT_SIZE)) * LOT_SIZE
        to_sell = min(to_sell, positions[code])
        if to_sell <= 0:
            continue

        gross = to_sell * price
        costs = _costs("sell", gross, config)
        proceeds = gross - costs["total"]
        cash += proceeds
        # Realised P&L against average cost, net of the sell-side costs.
        avg_cost = cost_basis.get(code, price)
        pnl = proceeds - avg_cost * to_sell
        positions[code] -= to_sell
        if positions[code] <= 0:
            positions.pop(code, None)
            cost_basis.pop(code, None)
        executed.append(
            {
                "code": code,
                "side": "sell",
                "trade_date": day,
                "price": price,
                "shares": to_sell,
                "gross": gross,
                "commission": costs["commission"],
                "stamp_tax": costs["stamp_tax"],
                "transfer_fee": costs["transfer_fee"],
                "slippage": float(bar["open"]) * to_sell * config.slippage_rate,
                "pnl": pnl,
                "reason": "rebalance",
            }
        )

    # --- 2) buys: names targeted but underweight ---
    for code, weight in clean_targets.items():
        bar = _bar(prices, code, day)
        if bar is None:
            executed.append(_skipped_row(day, code, "buy", "suspended"))
            continue

        allowed, reason = _is_tradeable(prices, code, day, "buy", config)
        if not allowed:
            executed.append(_skipped_row(day, code, "buy", reason))
            continue

        price = float(bar["open"]) * (1.0 + config.slippage_rate)
        if price <= 0:
            continue
        current_shares = positions.get(code, 0)
        current_value = current_shares * float(bar["close"])
        target_value = weight * equity
        shortfall = target_value - current_value
        if shortfall <= 0:
            continue

        # Reserve the cost of the order so cash never goes negative.
        budget = min(shortfall, cash)
        share_estimate = budget / price
        if share_estimate < LOT_SIZE:
            continue
        to_buy = int(np.floor(share_estimate / LOT_SIZE)) * LOT_SIZE

        # Shrink until the order plus costs fits within available cash.
        while to_buy > 0:
            gross = to_buy * price
            costs = _costs("buy", gross, config)
            if gross + costs["total"] <= cash:
                break
            to_buy -= LOT_SIZE
        if to_buy <= 0:
            continue

        gross = to_buy * price
        costs = _costs("buy", gross, config)
        cash -= gross + costs["total"]
        # Blend the new lot's all-in cost into the running average.
        previous_shares = current_shares
        new_shares = current_shares + to_buy
        previous_basis = cost_basis.get(code, 0.0) * previous_shares
        cost_basis[code] = (previous_basis + gross + costs["total"]) / new_shares
        positions[code] = new_shares
        locked[code] = day
        executed.append(
            {
                "code": code,
                "side": "buy",
                "trade_date": day,
                "price": price,
                "shares": to_buy,
                "gross": gross,
                "commission": costs["commission"],
                "stamp_tax": costs["stamp_tax"],
                "transfer_fee": costs["transfer_fee"],
                "slippage": float(bar["open"]) * to_buy * config.slippage_rate,
                "pnl": None,
                "reason": "rebalance",
            }
        )

    return cash, positions, locked, executed


def _skipped_row(day: date, code: str, side: str, reason: str) -> dict:
    """Record an order that could not be filled."""
    return {
        "code": code,
        "side": side,
        "trade_date": day,
        "price": 0.0,
        "shares": 0,
        "gross": 0.0,
        "commission": 0.0,
        "stamp_tax": 0.0,
        "transfer_fee": 0.0,
        "slippage": 0.0,
        "pnl": None,
        "reason": reason,
    }


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, config: BacktestConfig) -> dict:
    """Performance metrics from an equity curve and trade ledger."""
    if equity.empty:
        return {"error": "no equity curve"}

    values = equity["equity"].astype(float)
    initial = float(config.initial_cash)
    final = float(values.iloc[-1])
    total_return = final / initial - 1.0 if initial else 0.0

    days = len(equity)
    years = days / 243.0 if days else 0.0
    annualised = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0.2 else total_return

    daily_returns = values.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std(ddof=0) > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=0) * np.sqrt(243))
        downside = daily_returns[daily_returns < 0]
        sortino = (
            float(daily_returns.mean() / downside.std(ddof=0) * np.sqrt(243))
            if len(downside) > 1 and downside.std(ddof=0) > 0
            else None
        )
    else:
        sharpe = None
        sortino = None

    running_max = values.cummax()
    drawdowns = values / running_max - 1.0
    max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
    calmar = float(annualised / abs(max_drawdown)) if max_drawdown < 0 else None

    filled = trades[trades["shares"] > 0] if not trades.empty else trades
    buys = filled[filled["side"] == "buy"] if not filled.empty else filled
    sells = filled[filled["side"] == "sell"] if not filled.empty else filled
    skipped = trades[trades["shares"] == 0] if not trades.empty else trades

    total_costs = 0.0
    if not filled.empty:
        total_costs = float((filled["commission"] + filled["stamp_tax"] + filled["transfer_fee"]).sum())

    turnover = float(filled["gross"].sum() / initial) if not filled.empty and initial else 0.0

    benchmark_return = None
    if "benchmark" in equity and equity["benchmark"].notna().any():
        series = equity["benchmark"].dropna()
        if len(series) and initial:
            benchmark_return = float(series.iloc[-1] / initial - 1.0)

    skip_reasons: dict[str, int] = {}
    if not skipped.empty:
        skip_reasons = skipped["reason"].value_counts().to_dict()

    # Realised win rate over closed round-trips (sells carry P&L).
    win_rate = None
    profit_factor = None
    if not sells.empty and "pnl" in sells:
        realised = sells["pnl"].dropna()
        if len(realised):
            win_rate = float((realised > 0).mean())
            gross_profit = float(realised[realised > 0].sum())
            gross_loss = float(-realised[realised < 0].sum())
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    return {
        "initial_cash": initial,
        "final_equity": final,
        "total_return": total_return,
        "annualised_return": annualised,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "volatility": float(daily_returns.std(ddof=0) * np.sqrt(243)) if len(daily_returns) > 1 else None,
        "trading_days": days,
        "n_trades": len(filled),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "n_skipped": len(skipped),
        "skip_reasons": skip_reasons,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_costs": total_costs,
        "cost_share_of_initial": total_costs / initial if initial else 0.0,
        "turnover": turnover,
        "benchmark_return": benchmark_return,
        "excess_return": (total_return - benchmark_return if benchmark_return is not None else None),
    }


def buy_and_hold_signals(
    bars_by_symbol: dict[str, pd.DataFrame],
    code: str,
    start: date | None = None,
    weight: float = 1.0,
) -> dict[date, dict[str, float]]:
    """A trivial benchmark strategy: hold one name from the first available day."""
    frame = bars_by_symbol.get(code)
    if frame is None or frame.empty:
        return {}
    dates = sorted(pd.to_datetime(frame["trade_date"]).dt.date.tolist())
    if start is not None:
        dates = [d for d in dates if d >= start]
    if not dates:
        return {}
    # Signal one day before the first tradable day so execution lands on it.
    first = dates[0]
    earlier = [d for d in sorted(set(pd.to_datetime(frame["trade_date"]).dt.date)) if d < first]
    signal_date = earlier[-1] if earlier else first
    return {signal_date: {code: weight}}
