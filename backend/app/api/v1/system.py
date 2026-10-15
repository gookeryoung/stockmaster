"""Watchlist and system endpoints."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import canonical_code, db_session
from app.config import settings
from app.core.logging import get_logger
from app.core.symbols import normalize
from app.db.models import DailyBar, DataSyncTask, Instrument, TradeCalendar, WatchlistItem
from app.schemas import (
    HealthResponse,
    JobResult,
    ProviderStatus,
    SystemStatus,
    WatchlistItemIn,
    WatchlistItemOut,
)
from app.sync import repository as repo

log = get_logger(__name__)
router = APIRouter(tags=["system"])

DISCLAIMER = (
    "本工作台为量化研究工具，所有数据、指标、预测与回测结果仅供研究参考，"
    "不构成任何投资建议。历史表现不代表未来收益，据此交易的风险由使用者自行承担。"
)


@router.get("/health", response_model=HealthResponse)
def health():
    """Liveness probe."""
    return HealthResponse(status="ok", version=__version__)


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


@router.get("/watchlist", response_model=list[WatchlistItemOut])
def list_watchlist(
    list_name: str = Query(default="default"),
    session: Session = Depends(db_session),
):
    """Watchlist entries with current names resolved."""
    rows = session.execute(
        select(WatchlistItem)
        .where(WatchlistItem.list_name == list_name)
        .order_by(WatchlistItem.code)
    ).scalars().all()
    names = {
        i.code: i.name
        for i in session.execute(
            select(Instrument).where(Instrument.code.in_([r.code for r in rows]))
        ).scalars()
    } if rows else {}
    return [
        WatchlistItemOut(
            id=r.id, list_name=r.list_name, code=r.code,
            name=names.get(r.code, ""), note=r.note,
        )
        for r in rows
    ]


@router.post("/watchlist", response_model=WatchlistItemOut, status_code=status.HTTP_201_CREATED)
def add_watchlist_item(payload: WatchlistItemIn, session: Session = Depends(db_session)):
    """Add a symbol to a watchlist (idempotent on list+code)."""
    try:
        canon = normalize(payload.code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid code: {exc}",
        ) from exc

    existing = session.execute(
        select(WatchlistItem).where(
            WatchlistItem.list_name == payload.list_name, WatchlistItem.code == canon
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.note = payload.note
        session.commit()
        session.refresh(existing)
        item = existing
    else:
        item = WatchlistItem(
            list_name=payload.list_name, code=canon, note=payload.note
        )
        session.add(item)
        session.commit()
        session.refresh(item)

    instrument = repo.get_instrument(session, canon)
    return WatchlistItemOut(
        id=item.id, list_name=item.list_name, code=item.code,
        name=instrument.name if instrument else "", note=item.note,
    )


@router.delete("/watchlist/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_watchlist_item(item_id: int, session: Session = Depends(db_session)):
    """Remove a watchlist entry."""
    item = session.get(WatchlistItem, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found"
        )
    session.delete(item)
    session.commit()
    return None


# --------------------------------------------------------------------------- #
# System status and jobs
# --------------------------------------------------------------------------- #


@router.get("/system/status", response_model=SystemStatus)
def system_status(session: Session = Depends(db_session)):
    """Operational status: providers, scheduler, data coverage.

    Provider circuit-breaker state is surfaced here because silent failover is
    otherwise invisible, and a user needs to know when results are being served by
    a backup source.
    """
    from app.db.session import get_engine
    from app.jobs.scheduler import scheduler_status
    from app.providers.registry import reset_registry, get_registry

    try:
        provider_states = [ProviderStatus(**s) for s in get_registry().states()]
    except Exception as exc:  # noqa: BLE001
        log.warning("provider registry unavailable: %s", exc)
        provider_states = []

    bar_count = session.execute(select(func.count()).select_from(DailyBar)).scalar() or 0
    instrument_count = (
        session.execute(select(func.count()).select_from(Instrument)).scalar() or 0
    )
    latest_bar = session.execute(select(func.max(DailyBar.trade_date))).scalar()
    calendar_days = session.execute(
        select(func.count())
        .select_from(TradeCalendar)
        .where(TradeCalendar.is_open.is_(True))
    ).scalar() or 0

    tasks = session.execute(
        select(DataSyncTask).order_by(DataSyncTask.updated_at.desc()).limit(20)
    ).scalars().all()

    return SystemStatus(
        version=__version__,
        timezone=settings.timezone,
        database={
            "path": str(settings.db_path),
            "exists": settings.db_path.exists(),
            "size_mb": round(settings.db_path.stat().st_size / 1e6, 2)
            if settings.db_path.exists()
            else 0.0,
        },
        providers=provider_states,
        scheduler=scheduler_status(),
        data={
            "bar_count": bar_count,
            "instrument_count": instrument_count,
            "calendar_days": calendar_days,
            "latest_bar_date": str(latest_bar) if latest_bar else None,
            "watchlist_size": session.execute(
                select(func.count()).select_from(WatchlistItem)
            ).scalar() or 0,
            "sync_tasks": [
                {
                    "job": t.job,
                    "target": t.target,
                    "status": t.status,
                    "processed": t.processed,
                    "total": t.total,
                    "failed": t.failed,
                    "message": t.message,
                }
                for t in tasks
            ],
        },
        disclaimer=DISCLAIMER,
    )


@router.post("/system/jobs/{job_name}", response_model=JobResult)
def run_job(
    job_name: str,
    full: bool = False,
    horizon: int = Query(default=1, ge=1, le=60),
):
    """Trigger a pipeline job manually.

    Deliberately synchronous so the caller sees the actual outcome; a full-market
    backfill is better started from the CLI or scheduler.
    """
    from app.jobs import tasks

    mapping = {
        "calendar": lambda: tasks.job_sync_calendar(),
        "instruments": lambda: tasks.job_sync_instruments(),
        "index_daily": lambda: tasks.job_sync_index_daily(),
        "daily": lambda: tasks.job_sync_daily(full=full),
        "analytics": lambda: tasks.job_compute_analytics(),
        "train": lambda: tasks.job_train_models(horizon=horizon),
        "predict": lambda: tasks.job_predict(horizon=horizon),
        "alerts": lambda: tasks.job_scan_alerts(),
        "snapshot": lambda: tasks.job_snapshot_poll(),
        "pipeline": lambda: tasks.run_pipeline(),
    }

    if job_name not in mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown job {job_name!r}; available: {', '.join(sorted(mapping))}",
        )

    try:
        detail = mapping[job_name]()
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    return JobResult(job=job_name, detail=detail if isinstance(detail, dict) else {})
