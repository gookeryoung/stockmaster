"""Market and instrument data endpoints."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from stockmaster.api.deps import canonical_code, db_session, require_instrument
from stockmaster.core.logging import get_logger
from stockmaster.core.market import DASHBOARD_INDICES, is_limit_down, is_limit_up
from stockmaster.db.models import DailyBar, Instrument
from stockmaster.schemas import (
    BarOut,
    BarsResponse,
    CalendarResponse,
    IndexQuote,
    IndicatorSnapshot,
    InstrumentOut,
    InstrumentPage,
    MarketOverview,
)
from stockmaster.sync import repository as repo

log = get_logger(__name__)
router = APIRouter(tags=["market"])

#: Indices shown on the dashboard (shared with the sync job so the two cannot drift).
OVERVIEW_INDICES = DASHBOARD_INDICES


@router.get("/stocks", response_model=InstrumentPage)
def list_stocks(
    q: str | None = Query(default=None, description="按代码或名称搜索"),
    board: str | None = Query(default=None, description="主板/创业板/科创板/北交所"),
    include_indices: bool = False,
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
):
    """Search the security master."""
    items = repo.list_instruments(session, include_indices=include_indices, query=q)
    if board:
        items = [i for i in items if i.board == board]
    total = len(items)
    window = items[offset : offset + limit]
    return InstrumentPage(
        total=total,
        items=[InstrumentOut.model_validate(i) for i in window],
    )


@router.get("/stocks/{code}", response_model=InstrumentOut)
def get_stock(code: str, session: Session = Depends(db_session)):
    """Instrument detail (security master row)."""
    instrument = require_instrument(session, canonical_code(code))
    return InstrumentOut.model_validate(instrument)


@router.get("/stocks/{code}/bars", response_model=BarsResponse)
def get_bars(
    code: str,
    adjust: str = Query(default="qfq", pattern="^(raw|qfq|hfq)$"),
    start: date | None = None,
    end: date | None = None,
    limit: int | None = Query(default=500, ge=1, le=20000),
    session: Session = Depends(db_session),
):
    """Daily OHLCV bars.

    ``hfq`` is *derived* from raw data plus adjustment factors rather than stored,
    because it is reconstructable and would otherwise triple the storage.
    """
    canon = canonical_code(code)
    instrument = require_instrument(session, canon)

    if adjust == "hfq":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "后复权 (hfq) is derived; request raw or qfq instead. "
                "hfq is intentionally not materialised to keep storage bounded."
            ),
        )

    bars = repo.get_bars(session, canon, adjust, start=start, end=end, limit=limit)
    if not bars:
        # Tell the caller exactly what is missing and what to run. `have` lets the UI
        # (and a human) tell "never synced" apart from "only the other series exists",
        # which need different recovery commands.
        have = repo.available_adjusts(session, canon)
        sync_cmd = f"uv run stockmaster sync-daily --codes {canon}"
        if have:
            detail = f"{canon} 缺少 {adjust} 序列（现有：{', '.join(have)}）。请执行：{sync_cmd} 以补齐复权序列。"
        else:
            detail = (
                f"{canon} 尚未同步任何日线数据。请执行：{sync_cmd}，"
                f"或运行 `uv run stockmaster sync-daily --full` 回补全市场。"
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
            headers={
                # Lets a client distinguish "resource absent" from "wrong URL", so a
                # missing symbol does not read as a routing bug.
                "X-StockMaster-Reason": "data-not-synced",
                "X-StockMaster-Available-Adjusts": ",".join(have) if have else "",
            },
        )
    return BarsResponse(
        code=canon,
        name=instrument.name,
        adjust=adjust,
        count=len(bars),
        bars=[BarOut.model_validate(b) for b in bars],
    )


@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    start: date | None = None,
    end: date | None = None,
    session: Session = Depends(db_session),
):
    """Trading days in a range."""
    if start is None:
        start = date.today() - timedelta(days=90)
    if end is None:
        end = date.today() + timedelta(days=30)
    days = repo.list_calendar(session, start, end)
    return CalendarResponse(start=start, end=end, days=days)


@router.get("/market/overview", response_model=MarketOverview)
def market_overview(session: Session = Depends(db_session)):
    """Index levels plus market breadth, with data-freshness metadata."""
    indices: list[IndexQuote] = []
    for index_code, label in OVERVIEW_INDICES:
        bars = repo.get_bars(session, index_code, "raw", limit=2)
        if not bars:
            indices.append(IndexQuote(code=index_code, name=label))
            continue
        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else None
        preclose = latest.preclose or (previous.close if previous else None)
        pct_chg = (latest.close / preclose - 1.0) * 100.0 if preclose and preclose > 0 else latest.pct_chg
        indices.append(
            IndexQuote(
                code=index_code,
                name=label,
                close=latest.close,
                preclose=preclose,
                pct_chg=pct_chg,
                trade_date=latest.trade_date,
            )
        )

    # Breadth from the most recent stored bar date across all symbols.
    from sqlalchemy import func, select

    latest_date = session.execute(select(func.max(DailyBar.trade_date)).where(DailyBar.adjust == "qfq")).scalar()

    advancing = declining = unchanged = limit_up = limit_down = total = 0
    if latest_date is not None:
        rows = (
            session.execute(select(DailyBar).where(DailyBar.adjust == "qfq", DailyBar.trade_date == latest_date))
            .scalars()
            .all()
        )
        total = len(rows)
        for row in rows:
            change = row.pct_chg
            if change is None and row.preclose:
                change = (row.close / row.preclose - 1.0) * 100.0
            if change is None:
                unchanged += 1
                continue
            if change > 0.01:
                advancing += 1
            elif change < -0.01:
                declining += 1
            else:
                unchanged += 1
            if row.preclose:
                if is_limit_up(row.close, row.preclose, row.code, is_st=row.is_st):
                    limit_up += 1
                elif is_limit_down(row.close, row.preclose, row.code, is_st=row.is_st):
                    limit_down += 1

    freshness = {
        "latest_bar_date": str(latest_date) if latest_date else None,
        "bar_count": repo.bar_count(session),
        "instrument_count": session.execute(select(func.count()).select_from(Instrument)).scalar(),
    }

    return MarketOverview(
        as_of=latest_date,
        indices=indices,
        advancing=advancing,
        declining=declining,
        unchanged=unchanged,
        limit_up=limit_up,
        limit_down=limit_down,
        total=total,
        data_freshness=freshness,
    )


@router.get("/stocks/{code}/indicators", response_model=IndicatorSnapshot)
def get_indicators(
    code: str,
    limit: int = Query(default=260, ge=30, le=2000),
    session: Session = Depends(db_session),
):
    """Technical indicators for the latest bar.

    Computed on demand from stored bars rather than read from the indicator table,
    so the response always matches the current indicator definitions.
    """
    canon = canonical_code(code)
    require_instrument(session, canon)

    bars = repo.get_bars(session, canon, "qfq", limit=limit)
    if len(bars) < 30:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"insufficient history for {canon}: {len(bars)} bars (need >= 30)",
        )

    import pandas as pd

    from stockmaster.indicators.compute import compute_indicators

    frame = pd.DataFrame(
        {
            "trade_date": [b.trade_date for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    indicators = compute_indicators(frame)
    last = indicators.iloc[-1]
    values = {column: (None if pd.isna(last[column]) else float(last[column])) for column in indicators.columns}
    return IndicatorSnapshot(code=canon, trade_date=bars[-1].trade_date, values=values)


@router.get("/stocks/{code}/snapshot")
def get_snapshot(code: str, session: Session = Depends(db_session)):
    """Intraday snapshot from the fallback provider chain (best effort)."""
    canon = canonical_code(code)
    require_instrument(session, canon)
    from stockmaster.providers.registry import get_registry

    try:
        snapshots = get_registry().fetch_snapshot([canon])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"snapshot unavailable: {exc}",
        ) from exc
    if not snapshots:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no snapshot returned")
    snap = snapshots[0]
    return {
        "code": snap.code,
        "name": snap.name,
        "price": snap.price,
        "preclose": snap.preclose,
        "open": snap.open,
        "high": snap.high,
        "low": snap.low,
        "volume": snap.volume,
        "amount": snap.amount,
        "pct_chg": snap.pct_chg,
        "quote_time": snap.quote_time,
        "source": snap.source,
    }
