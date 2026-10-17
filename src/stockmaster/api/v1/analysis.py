"""Analysis endpoints: factor snapshots, screening and correlation."""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.api.deps import canonical_code, db_session, require_instrument
from stockmaster.core.logging import get_logger
from stockmaster.db.models import Factor
from stockmaster.schemas import (
    CorrelationResponse,
    FactorSnapshot,
    ScreenRequest,
    ScreenRow,
)

log = get_logger(__name__)
router = APIRouter(tags=["analysis"])

#: Factor columns exposed to clients.
FACTOR_FIELDS = (
    "mom_5",
    "mom_20",
    "mom_60",
    "mom_120",
    "dist_high_252",
    "vol_20",
    "vol_60",
    "atr_pct",
    "turnover_20",
    "amount_log_20",
    "amihud_20",
    "ma_bull",
    "breakout_20",
    "rsi_14",
    "z_mom_20",
    "z_mom_60",
    "z_vol_20",
    "z_turnover_20",
    "z_rsi_14",
)
PERCENTILE_FIELDS = ("pct_mom_20", "pct_mom_60")


@router.get("/analysis/factors/{code}", response_model=FactorSnapshot)
def get_factors(code: str, session: Session = Depends(db_session)):
    """Latest stored factor values for one symbol.

    Served from the factor table (written by the analytics job) rather than
    recomputed, because cross-sectional z-scores are only meaningful when a whole
    date's universe has been processed together.
    """
    canon = canonical_code(code)
    instrument = require_instrument(session, canon)

    row = session.execute(
        select(Factor).where(Factor.code == canon).order_by(Factor.trade_date.desc()).limit(1)
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"no factors stored for {canon}; run the analytics job (POST /system/jobs/analytics) first"),
        )

    return FactorSnapshot(
        code=canon,
        name=instrument.name,
        trade_date=row.trade_date,
        factors={field: getattr(row, field) for field in FACTOR_FIELDS},
        score=row.score,
        percentiles={field: getattr(row, field) for field in PERCENTILE_FIELDS},
    )


@router.post("/analysis/screen", response_model=list[ScreenRow])
def screen(payload: ScreenRequest, session: Session = Depends(db_session)):
    """Rank the stored universe by composite factor score with explicit filters."""
    stmt = select(Factor).order_by(Factor.score.desc().nullslast())
    if payload.trade_date is not None:
        stmt = stmt.where(Factor.trade_date == payload.trade_date)

    if payload.trade_date is None:
        # Default to the most recent date that actually has factors.
        latest = session.execute(select(Factor.trade_date).order_by(Factor.trade_date.desc()).limit(1)).scalar()
        if latest is None:
            return []
        stmt = select(Factor).where(Factor.trade_date == latest).order_by(Factor.score.desc().nullslast())

    rows = list(session.execute(stmt).scalars())

    # Join instrument metadata for names, boards and ST filtering.
    from stockmaster.db.models import Instrument

    instruments = {
        i.code: i
        for i in session.execute(select(Instrument).where(Instrument.code.in_([r.code for r in rows]))).scalars()
    }

    out: list[ScreenRow] = []
    for row in rows:
        instrument = instruments.get(row.code)
        if instrument is None:
            continue
        if payload.exclude_st and instrument.is_st:
            continue
        if payload.boards and instrument.board not in payload.boards:
            continue
        if payload.min_score is not None and (row.score is None or row.score < payload.min_score):
            continue
        if payload.max_vol is not None and row.vol_20 is not None and row.vol_20 > payload.max_vol:
            continue
        if (
            payload.min_amount_log is not None
            and row.amount_log_20 is not None
            and row.amount_log_20 < payload.min_amount_log
        ):
            continue

        out.append(
            ScreenRow(
                code=row.code,
                name=instrument.name,
                trade_date=row.trade_date,
                score=row.score,
                mom_20=row.mom_20,
                mom_60=row.mom_60,
                vol_20=row.vol_20,
                turnover_20=row.turnover_20,
                rsi_14=row.rsi_14,
            )
        )
        if len(out) >= payload.limit:
            break

    return out


@router.get("/analysis/correlation", response_model=CorrelationResponse)
def correlation(
    codes: str = Query(description="逗号分隔的证券代码"),
    window: int = Query(default=120, ge=20, le=500),
    session: Session = Depends(db_session),
):
    """Close-price correlation matrix over a rolling window."""
    requested = [c.strip() for c in codes.split(",") if c.strip()]
    if len(requested) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide at least two comma-separated codes",
        )
    if len(requested) > 30:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="at most 30 codes may be compared at once",
        )

    from stockmaster.sync import repository as repo

    series: dict[str, pd.Series] = {}
    for raw in requested:
        canon = canonical_code(raw)
        bars = repo.get_bars(session, canon, "qfq", limit=window)
        if len(bars) < 20:
            continue
        series[canon] = pd.Series(
            [b.close for b in bars],
            index=[b.trade_date for b in bars],
        )

    if len(series) < 2:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="fewer than two symbols had enough stored history",
        )

    frame = pd.DataFrame(series).sort_index().dropna(how="all")
    correlations = frame.pct_change().corr()

    kept = list(correlations.columns)
    matrix: list[list[float | None]] = []
    for row_code in kept:
        row: list[float | None] = []
        for col_code in kept:
            value = correlations.loc[row_code, col_code]
            row.append(None if pd.isna(value) else float(value))
        matrix.append(row)

    return CorrelationResponse(codes=kept, matrix=matrix)
