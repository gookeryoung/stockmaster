"""Backtest endpoints: run a strategy and inspect the stored result."""

from __future__ import annotations

import json
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.api.deps import db_session
from stockmaster.backtest.engine import BacktestConfig, run_backtest
from stockmaster.config import settings
from stockmaster.core.logging import get_logger
from stockmaster.db.models import BacktestEquity, BacktestRun, BacktestTrade
from stockmaster.db.scoped import get_in_workspace_or_404, in_workspace
from stockmaster.db.session import session_scope
from stockmaster.schemas import (
    BacktestRequest,
    BacktestResponse,
    BacktestSummary,
    EquityPoint,
    TradeOut,
)
from stockmaster.workspace.deps import WorkspaceCtx, current_workspace, require_editor

log = get_logger(__name__)
router = APIRouter(tags=["backtest"])

DISCLAIMER = (
    "历史回测不代表未来收益。结果已扣除佣金（含最低 5 元）、印花税（卖出）、"
    "过户费与滑点，并强制执行 T+1 与涨跌停不可成交限制。仅供研究参考。"
)


def _load_bars(session: Session, codes: list[str], adjust: str = "qfq") -> dict:
    from stockmaster.sync import repository as repo

    out = {}
    for code in codes:
        bars = repo.get_bars(session, code, adjust)
        if not bars:
            continue
        out[code] = __import__("pandas").DataFrame(
            {
                "trade_date": [b.trade_date for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "preclose": [b.preclose for b in bars],
                "volume": [b.volume for b in bars],
                "amount": [b.amount for b in bars],
                "is_st": [b.is_st for b in bars],
            }
        )
    return out


def _default_codes(
    session: Session,  # noqa: ARG001 - kept for call-site symmetry
    limit: int = 50,
    workspace_id: int | None = None,
) -> list[str]:
    from stockmaster.jobs.tasks import _default_universe

    codes = _default_universe(workspace_id=workspace_id)
    return codes[:limit]


def _build_signals(payload: BacktestRequest, session: Session, bars: dict) -> dict[date, dict[str, float]]:
    """Translate a strategy choice into a date -> weights mapping.

    Every strategy reads point-in-time data only: the score/prediction stored for
    date ``t`` is turned into a signal dated ``t``, and the engine executes it at
    ``t+1``.
    """
    codes = list(bars)
    if payload.strategy == "buy_hold":
        from stockmaster.backtest.engine import buy_and_hold_signals

        if not codes:
            return {}
        return buy_and_hold_signals(bars, codes[0], start=payload.start, weight=1.0)

    if payload.strategy == "ma_cross":
        signals: dict[date, dict[str, float]] = {}
        for code, frame in bars.items():
            frame = frame.sort_values("trade_date").reset_index(drop=True)
            close = frame["close"].astype(float)
            fast = close.rolling(5, min_periods=5).mean()
            slow = close.rolling(20, min_periods=20).mean()
            above = (fast > slow).to_numpy()
            dates = list(frame["trade_date"])
            all_days = sorted(dates)
            for index in range(1, len(dates)):
                if above[index] and not above[index - 1]:
                    signal_date = all_days[index - 1]
                    signals.setdefault(signal_date, {})[code] = 1.0
                elif not above[index] and above[index - 1]:
                    signal_date = all_days[index - 1]
                    signals.setdefault(signal_date, {}).pop(code, None)
        return signals

    if payload.strategy == "score_top_n":
        # Recomputed from bars rather than read from the thin `factors` cache,
        # which holds only the most recent date per symbol and would therefore
        # produce a single rebalance point.
        from stockmaster.backtest.signals import build_score_history, top_n_per_date

        history = build_score_history(bars)
        if history.empty:
            return {}
        return _thin_by_cadence(top_n_per_date(history, payload.top_n), payload.rebalance_days)

    if payload.strategy == "prediction_top_n":
        from stockmaster.db.models import Prediction

        rows = session.execute(select(Prediction).order_by(Prediction.trade_date.asc())).scalars().all()
        by_date: dict[date, list[tuple[float, str]]] = {}
        for row in rows:
            if row.prob_up is None or row.code not in bars:
                continue
            by_date.setdefault(row.trade_date, []).append((row.prob_up, row.code))

        signals: dict[date, dict[str, float]] = {}
        for day in sorted(by_date):
            ranked = sorted(by_date[day], reverse=True)[: payload.top_n]
            if not ranked:
                continue
            weight = 1.0 / len(ranked)
            signals[day] = {code: weight for _p, code in ranked}
        return _thin_by_cadence(signals, payload.rebalance_days)

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"unknown strategy {payload.strategy!r}",
    )


def _thin_by_cadence(signals: dict[date, dict[str, float]], rebalance_days: int) -> dict[date, dict[str, float]]:
    """Keep only the first signal in each ``rebalance_days`` window.

    Without this, a daily score would trade every day — inflating turnover and
    misrepresenting a strategy that is meant to rebalance periodically.
    """
    if rebalance_days <= 1 or not signals:
        return signals
    kept: dict[date, dict[str, float]] = {}
    last: date | None = None
    for day in sorted(signals):
        if last is None or (day - last).days >= rebalance_days:
            kept[day] = signals[day]
            last = day
    return kept


@router.post("/backtest/run", response_model=BacktestResponse)
def run(
    payload: BacktestRequest,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Execute a backtest and persist the run, equity curve and trade ledger.

    Editor and above: a run consumes CPU for minutes and writes into the shared
    history, so a read-only member must not be able to trigger it.
    """
    codes = [c.strip() for c in payload.codes if c.strip()]
    if not codes and payload.screening_id is not None:
        # 显式 codes 优先；只有为空时才用工作区的选股配置解析标的池。
        from stockmaster.strategy.service import run_screening_config

        screening = run_screening_config(session, payload.screening_id, ws.id)
        codes = [item.code for item in screening.items]
        if not codes:
            # 明确指定了选股配置却一只都没命中：静默回退默认宇宙会让用户以为在测
            # 自己的策略，实际测的是别的东西。宁可 409。
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the screening config produced no candidates; adjust its filters or sync analytics first",
            )
    if not codes:
        # 不指定标的时，「默认宇宙」在隔离模式下是本工作区的自选，而不是全站并集。
        codes = _default_codes(session, limit=50, workspace_id=ws.id if settings.data_isolation else None)
    if not codes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no universe available; sync data first or check the screening config",
        )

    adjust = "qfq"
    bars = _load_bars(session, codes, adjust)
    if not bars:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no stored bars for the requested codes",
        )

    # Benchmark is a separate series, not a tradable member of the universe.
    benchmark_code = payload.benchmark
    if benchmark_code and benchmark_code not in bars:
        extra = _load_bars(session, [benchmark_code], "raw")
        bars.update(extra)

    config = BacktestConfig(
        initial_cash=payload.initial_cash,
        commission_rate=payload.commission_rate,
        min_commission=payload.min_commission,
        stamp_tax_rate=payload.stamp_tax_rate,
        transfer_fee_rate=payload.transfer_fee_rate,
        slippage_rate=payload.slippage_rate,
        exclude_st=payload.exclude_st,
        max_position_weight=payload.max_position_weight,
    )

    signals = _build_signals(payload, session, bars)
    if not signals:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the selected strategy produced no signals; it may need factor or "
                "prediction data (run the analytics/predict jobs first)"
            ),
        )

    result = run_backtest(
        bars,
        signals,
        config=config,
        start=payload.start,
        end=payload.end,
        benchmark=benchmark_code,
    )

    run_id = str(uuid.uuid4())
    _persist_run(run_id, payload, result, config, signals, workspace_id=ws.id)

    return BacktestResponse(
        run_id=run_id,
        strategy=payload.strategy,
        metrics=result.metrics,
        warnings=result.warnings,
        equity=[
            EquityPoint(
                trade_date=row.trade_date,
                equity=row.equity,
                cash=row.cash,
                market_value=row.market_value,
                drawdown=row.drawdown,
                benchmark=row.benchmark,
            )
            for row in result.equity.itertuples()
        ],
        trades=[
            TradeOut(
                code=row.code,
                side=row.side,
                trade_date=row.trade_date,
                price=row.price,
                shares=row.shares,
                gross=row.gross,
                commission=row.commission,
                stamp_tax=row.stamp_tax,
                transfer_fee=row.transfer_fee,
                slippage=row.slippage,
                pnl=row.pnl,
                reason=row.reason,
            )
            for row in result.trades.itertuples()
        ],
        disclaimer=DISCLAIMER,
    )


def _persist_run(
    run_id: str,
    payload: BacktestRequest,
    result,
    config,  # noqa: ARG001 - costs already reflected in result.metrics
    signals,  # noqa: ARG001 - persisted via payload for reproducibility
    *,
    workspace_id: int = 0,
) -> None:
    """Store the run summary, equity curve and trades.

    The parent ``backtest_runs`` row is flushed before the children are inserted:
    the child tables carry a foreign key to it, and ``bulk_save_objects`` bypasses
    the unit-of-work ordering that would otherwise insert the parent first.
    """
    with session_scope() as session:
        session.add(
            BacktestRun(
                id=run_id,
                workspace_id=workspace_id,
                name=f"{payload.strategy} {date.today()}",
                strategy=payload.strategy,
                universe=",".join(payload.codes[:50]) if payload.codes else "default",
                start_date=payload.start,
                end_date=payload.end,
                initial_cash=payload.initial_cash,
                params_json=json.dumps(payload.model_dump(), default=str),
                metrics_json=json.dumps(result.metrics, default=str),
                cost_json=json.dumps(result.config, default=str),
                status="done",
            )
        )
        # Emit the INSERT now so the FK target exists for the rows below.
        session.flush()

        if not result.equity.empty:
            session.add_all(
                [
                    BacktestEquity(
                        run_id=run_id,
                        trade_date=row.trade_date,
                        equity=row.equity,
                        cash=row.cash,
                        market_value=row.market_value,
                        drawdown=row.drawdown,
                        benchmark=row.benchmark,
                    )
                    for row in result.equity.itertuples()
                ]
            )
        if not result.trades.empty:
            session.add_all(
                [
                    BacktestTrade(
                        run_id=run_id,
                        code=row.code,
                        side=row.side,
                        trade_date=row.trade_date,
                        price=row.price,
                        shares=row.shares,
                        gross=row.gross,
                        commission=row.commission,
                        stamp_tax=row.stamp_tax,
                        transfer_fee=row.transfer_fee,
                        slippage=row.slippage,
                        pnl=row.pnl,
                        reason=row.reason,
                    )
                    for row in result.trades.itertuples()
                ]
            )


@router.get("/backtest", response_model=list[BacktestSummary])
def list_runs(
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """Recent backtest runs of the current workspace."""
    stmt = in_workspace(select(BacktestRun), BacktestRun, ws.id).order_by(BacktestRun.created_at.desc()).limit(limit)
    rows = session.execute(stmt).scalars()
    return [
        BacktestSummary(
            run_id=row.id,
            strategy=row.strategy,
            status=row.status,
            created_at=row.created_at,
            metrics=json.loads(row.metrics_json or "{}"),
        )
        for row in rows
    ]


@router.get("/backtest/{run_id}")
def get_run(
    run_id: str,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """Full stored result for a run."""
    row = get_in_workspace_or_404(session, BacktestRun, run_id, ws.id, "run")
    equity = session.execute(
        select(BacktestEquity).where(BacktestEquity.run_id == run_id).order_by(BacktestEquity.trade_date)
    ).scalars()
    trades = session.execute(
        select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.trade_date)
    ).scalars()

    return {
        "run_id": row.id,
        "strategy": row.strategy,
        "status": row.status,
        "created_at": row.created_at,
        "metrics": json.loads(row.metrics_json or "{}"),
        "config": json.loads(row.cost_json or "{}"),
        "equity": [
            {
                "trade_date": point.trade_date,
                "equity": point.equity,
                "cash": point.cash,
                "market_value": point.market_value,
                "drawdown": point.drawdown,
                "benchmark": point.benchmark,
            }
            for point in equity
        ],
        "trades": [
            {
                "code": t.code,
                "side": t.side,
                "trade_date": t.trade_date,
                "price": t.price,
                "shares": t.shares,
                "gross": t.gross,
                "commission": t.commission,
                "stamp_tax": t.stamp_tax,
                "transfer_fee": t.transfer_fee,
                "slippage": t.slippage,
                "pnl": t.pnl,
                "reason": t.reason,
            }
            for t in trades
        ],
        "disclaimer": DISCLAIMER,
    }
