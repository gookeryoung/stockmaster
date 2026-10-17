"""Watchlist and system endpoints."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockmaster import __version__
from stockmaster.config import settings
from stockmaster.core import quota
from stockmaster.core.auth_deps import require_admin
from stockmaster.core.deps import db_session
from stockmaster.core.logging import get_logger
from stockmaster.core.symbols import normalize
from stockmaster.core.workspace_deps import WorkspaceCtx, current_workspace, require_editor
from stockmaster.db.models import DailyBar, DataSyncTask, Instrument, TradeCalendar, WatchlistItem
from stockmaster.db.scoped import get_in_workspace_or_404, in_workspace
from stockmaster.schemas import (
    HealthResponse,
    JobResult,
    ProviderStatus,
    SystemStatus,
    WatchlistItemIn,
    WatchlistItemOut,
)
from stockmaster.sync import repository as repo

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
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """Watchlist entries in the current workspace, with names resolved."""
    stmt = in_workspace(select(WatchlistItem), WatchlistItem, ws.id)
    rows = (
        session.execute(stmt.where(WatchlistItem.list_name == list_name).order_by(WatchlistItem.code)).scalars().all()
    )
    names = (
        {
            i.code: i.name
            for i in session.execute(select(Instrument).where(Instrument.code.in_([r.code for r in rows]))).scalars()
        }
        if rows
        else {}
    )
    return [
        WatchlistItemOut(
            id=r.id,
            list_name=r.list_name,
            code=r.code,
            name=names.get(r.code, ""),
            note=r.note,
        )
        for r in rows
    ]


@router.post("/watchlist", response_model=WatchlistItemOut, status_code=status.HTTP_201_CREATED)
def add_watchlist_item(
    payload: WatchlistItemIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Add a symbol to a watchlist (idempotent on list+code).

    Requires at least editor: a viewer must not be able to change what everybody
    else in the workspace sees.
    """
    try:
        canon = normalize(payload.code)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid code: {exc}",
        ) from exc

    # 归属条件必须出现在「查重」这一步：隔离开启时，同一个 list_name+code 在不同
    # 工作区下是两条独立记录，漏掉过滤会直接改写别的工作区的 note。
    existing = session.execute(
        in_workspace(select(WatchlistItem), WatchlistItem, ws.id).where(
            WatchlistItem.list_name == payload.list_name, WatchlistItem.code == canon
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.note = payload.note
        session.commit()
        session.refresh(existing)
        item = existing
    else:
        # 只有「新增条目」占额度；幂等更新已有条目（上面分支）不消耗配额。
        quota.enforce(session, ws.id, "watchlist_items")
        item = WatchlistItem(workspace_id=ws.id, list_name=payload.list_name, code=canon, note=payload.note)
        session.add(item)
        session.commit()
        session.refresh(item)

    instrument = repo.get_instrument(session, canon)
    return WatchlistItemOut(
        id=item.id,
        list_name=item.list_name,
        code=item.code,
        name=instrument.name if instrument else "",
        note=item.note,
    )


@router.delete("/watchlist/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_watchlist_item(
    item_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Remove a watchlist entry (another workspace's entry is indistinguishable from absent)."""
    item = get_in_workspace_or_404(session, WatchlistItem, item_id, ws.id, "item")
    session.delete(item)
    session.commit()


# --------------------------------------------------------------------------- #
# System status and jobs
# --------------------------------------------------------------------------- #


@router.get("/system/status", response_model=SystemStatus)
def system_status(
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """Operational status: providers, scheduler, data coverage.

    Provider circuit-breaker state is surfaced here because silent failover is
    otherwise invisible, and a user needs to know when results are being served by
    a backup source.
    """
    from stockmaster.jobs.scheduler import scheduler_status
    from stockmaster.providers.registry import get_registry

    try:
        provider_states = [ProviderStatus(**s) for s in get_registry().states()]
    except Exception as exc:
        log.warning("provider registry unavailable: %s", exc)
        provider_states = []

    bar_count = session.execute(select(func.count()).select_from(DailyBar)).scalar() or 0
    instrument_count = session.execute(select(func.count()).select_from(Instrument)).scalar() or 0
    latest_bar = session.execute(select(func.max(DailyBar.trade_date))).scalar()
    calendar_days = (
        session.execute(select(func.count()).select_from(TradeCalendar).where(TradeCalendar.is_open.is_(True))).scalar()
        or 0
    )

    tasks = session.execute(select(DataSyncTask).order_by(DataSyncTask.updated_at.desc()).limit(20)).scalars().all()

    return SystemStatus(
        version=__version__,
        timezone=settings.timezone,
        database={
            "path": str(settings.db_path),
            "exists": settings.db_path.exists(),
            "size_mb": round(settings.db_path.stat().st_size / 1e6, 2) if settings.db_path.exists() else 0.0,
        },
        providers=provider_states,
        scheduler=scheduler_status(),
        data={
            "bar_count": bar_count,
            "instrument_count": instrument_count,
            "calendar_days": calendar_days,
            "latest_bar_date": str(latest_bar) if latest_bar else None,
            # 隔离开启时是「我的自选数」；关闭时条件退化为恒真，仍是全表计数
            "watchlist_size": session.execute(
                in_workspace(select(func.count()).select_from(WatchlistItem), WatchlistItem, ws.id)
            ).scalar()
            or 0,
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


@router.get("/system/data-quality")
def data_quality(
    adjust: str = Query(default="qfq", pattern="^(qfq|raw)$"),
    codes: str | None = Query(default=None, description="逗号分隔的代码；留空按最近更新抽样"),
    max_symbols: int = Query(default=200, ge=1, le=5000),
    stale_days: int = Query(default=5, ge=0, le=365),
    only_problems: bool = Query(default=False),
    session: Session = Depends(db_session),
):
    """Data quality report: coverage, gaps, staleness, anomalies, adjustment breaks.

    Returns whole-market coverage counts plus bounded per-symbol detail. Pass
    ``only_problems=true`` to keep the payload small when polling.
    """
    from stockmaster.sync.quality import check_data_quality

    code_list = [c.strip() for c in (codes or "").split(",") if c.strip()]
    report = check_data_quality(
        session,
        codes=code_list or None,
        adjust=adjust,
        stale_days=stale_days,
        max_symbols=max_symbols,
    )
    return report.as_dict(only_problems=only_problems)


@router.post(
    "/system/jobs/{job_name}",
    response_model=JobResult,
    dependencies=[Depends(require_admin)],
)
def run_job(
    job_name: str,
    full: bool = False,
    horizon: int = Query(default=1, ge=1, le=60),
):
    """Trigger a pipeline job manually.

    Deliberately synchronous so the caller sees the actual outcome; a full-market
    backfill is better started from the CLI or scheduler.
    """
    from stockmaster.jobs import tasks

    # partial (rather than lambda) keeps each entry a direct reference to the
    # job function, so the mapping is data instead of deferred code.
    mapping = {
        "calendar": tasks.job_sync_calendar,
        "instruments": tasks.job_sync_instruments,
        "index_daily": tasks.job_sync_index_daily,
        "daily": partial(tasks.job_sync_daily, full=full),
        "analytics": tasks.job_compute_analytics,
        "train": partial(tasks.job_train_models, horizon=horizon),
        "predict": partial(tasks.job_predict, horizon=horizon),
        "alerts": tasks.job_scan_alerts,
        "workspace_usage": tasks.job_snapshot_workspace_usage,
        "snapshot": tasks.job_snapshot_poll,
        "pipeline": tasks.run_pipeline,
    }

    if job_name not in mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown job {job_name!r}; available: {', '.join(sorted(mapping))}",
        )

    try:
        detail = mapping[job_name]()
    except Exception as exc:
        log.exception("job %s failed", job_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    return JobResult(job=job_name, detail=detail if isinstance(detail, dict) else {})
