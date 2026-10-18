"""Watchlist and system endpoints."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from stockmaster import __version__
from stockmaster.config import settings
from stockmaster.core import quota
from stockmaster.core.auth_deps import require_admin
from stockmaster.core.deps import db_session
from stockmaster.core.errors import DataError
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
    WatchlistBatchDeleteIn,
    WatchlistBatchDeleteOut,
    WatchlistImportIn,
    WatchlistImportInvalid,
    WatchlistImportOut,
    WatchlistItemIn,
    WatchlistItemOut,
    WatchlistListDeleteOut,
    WatchlistListOut,
    WatchlistListRenameIn,
    WatchlistListRenameOut,
    WatchlistQuotaInfo,
)
from stockmaster.sync import repository as repo

log = get_logger(__name__)
router = APIRouter(tags=["system"])

DISCLAIMER = (
    "本工作台为量化研究工具，所有数据、指标、预测与回测结果仅供研究参考，"
    "不构成任何投资建议。历史表现不代表未来收益，据此交易的风险由使用者自行承担。"
)


# --------------------------------------------------------------------------- #
# Watchlist helpers
# --------------------------------------------------------------------------- #


def _http_error(status_code: int, detail: str, code: str) -> HTTPException:
    """Domain error in the project's usual shape (``detail`` + ``X-Auth-Error``)."""
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Auth-Error": code})


def _clean_list_name(raw: str) -> str:
    """Validate a user-supplied list name (empty/too long/control chars are 422)."""
    name = raw.strip()
    if not name:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "清单名不能为空", "invalid_name")
    if len(name) > 64:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "清单名最多 64 个字符", "invalid_name")
    if any(ord(ch) < 32 for ch in name):
        raise _http_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "清单名不能包含控制字符", "invalid_name")
    return name


def _list_exists(session: Session, workspace_id: int, list_name: str) -> bool:
    stmt = in_workspace(
        select(WatchlistItem.id).where(WatchlistItem.list_name == list_name),
        WatchlistItem,
        workspace_id,
    ).limit(1)
    return session.execute(stmt).first() is not None


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


@router.get("/watchlist/lists", response_model=list[WatchlistListOut])
def list_watchlist_lists(
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """Enumerate the workspace's watchlists with counts.

    Lists are derived from ``watchlist_items.list_name`` (there is no list table),
    so an empty list cannot exist. ``default`` is always returned first, and a
    workspace with no rows at all gets a synthetic empty ``default`` entry so the
    UI has somewhere to add its first stock.
    """
    stmt = in_workspace(
        select(
            WatchlistItem.list_name,
            func.count(WatchlistItem.id),
            func.max(WatchlistItem.added_at),
        ).group_by(WatchlistItem.list_name),
        WatchlistItem,
        ws.id,
    )
    rows = session.execute(stmt).all()
    if not rows:
        return [WatchlistListOut(list_name="default", count=0, last_added_at=None)]

    items = [
        WatchlistListOut(list_name=str(name), count=int(count), last_added_at=last_added_at)
        for name, count, last_added_at in rows
    ]
    # default 固定在第一位；其余按条目数降序、名称升序，前端无需再排序。
    items.sort(key=lambda item: (0 if item.list_name == "default" else 1, -item.count, item.list_name))
    return items


@router.patch("/watchlist/lists/{list_name}", response_model=WatchlistListRenameOut)
def rename_watchlist_list(
    list_name: str,
    payload: WatchlistListRenameIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Rename a derived list by updating ``list_name`` on all its rows.

    References from alert rules / sample configs are *not* migrated: they store
    the list name as plain text, so the UI warns about them before renaming.
    """
    old_name = list_name
    new_name = _clean_list_name(payload.new_name)
    if not _list_exists(session, ws.id, old_name):
        raise _http_error(status.HTTP_404_NOT_FOUND, f"清单 {old_name!r} 不存在", "list_not_found")
    if new_name == old_name:
        count = session.execute(
            in_workspace(
                select(func.count(WatchlistItem.id)).where(WatchlistItem.list_name == old_name),
                WatchlistItem,
                ws.id,
            )
        ).scalar_one()
        return WatchlistListRenameOut(old_name=old_name, new_name=new_name, updated=int(count))
    if _list_exists(session, ws.id, new_name):
        raise _http_error(status.HTTP_409_CONFLICT, f"清单 {new_name!r} 已存在", "list_exists")

    result = session.execute(
        in_workspace(
            update(WatchlistItem).where(WatchlistItem.list_name == old_name).values(list_name=new_name),
            WatchlistItem,
            ws.id,
        )
    )
    session.commit()
    return WatchlistListRenameOut(old_name=old_name, new_name=new_name, updated=int(result.rowcount or 0))


@router.delete("/watchlist/lists/{list_name}", response_model=WatchlistListDeleteOut)
def delete_watchlist_list(
    list_name: str,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Delete every row in a derived list. Alert rules / backtests are untouched."""
    if not _list_exists(session, ws.id, list_name):
        raise _http_error(status.HTTP_404_NOT_FOUND, f"清单 {list_name!r} 不存在", "list_not_found")
    result = session.execute(
        in_workspace(
            delete(WatchlistItem).where(WatchlistItem.list_name == list_name),
            WatchlistItem,
            ws.id,
        )
    )
    session.commit()
    return WatchlistListDeleteOut(list_name=list_name, deleted=int(result.rowcount or 0))


@router.post("/watchlist/batch-delete", response_model=WatchlistBatchDeleteOut)
def batch_delete_watchlist_items(
    payload: WatchlistBatchDeleteIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Delete many entries in one transaction.

    Ids that do not exist *or belong to another workspace* are reported as
    ``missing`` with a 200 — matching the per-item endpoint's 404 semantics, which
    deliberately cannot distinguish "absent" from "somebody else's".
    """
    ids = list(dict.fromkeys(payload.ids))
    rows = list(
        session.execute(
            in_workspace(
                select(WatchlistItem).where(WatchlistItem.id.in_(ids)),
                WatchlistItem,
                ws.id,
            )
        )
        .scalars()
        .all()
    )
    found = {row.id for row in rows}
    missing = [item_id for item_id in ids if item_id not in found]
    if found:
        session.execute(
            in_workspace(
                delete(WatchlistItem).where(WatchlistItem.id.in_(found)),
                WatchlistItem,
                ws.id,
            )
        )
        session.commit()
    return WatchlistBatchDeleteOut(deleted=len(found), missing=missing)


@router.post("/watchlist/import", response_model=WatchlistImportOut)
def import_watchlist_items(
    payload: WatchlistImportIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """Batch add / upsert used by CSV import and “选股结果 → 自选”.

    One request = one transaction. Invalid codes are reported per input index and
    do not abort the rest; the quota is checked exactly once against the number of
    genuinely new rows, so a 409 never leaves a half-imported list behind.
    """
    list_name = _clean_list_name(payload.list_name)
    existed_before = _list_exists(session, ws.id, list_name)
    if not payload.create_list and not existed_before:
        raise _http_error(status.HTTP_404_NOT_FOUND, f"清单 {list_name!r} 不存在", "list_not_found")

    # 备注按键规范化后合并；超过 256 字符属于请求级错误，整批拒绝。
    notes: dict[str, str] = {}
    for raw_key, note in payload.notes.items():
        if len(note) > 256:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "备注最多 256 个字符",
                "invalid_notes",
            )
        try:
            canon_key = normalize(raw_key)
        except DataError:
            continue
        notes.setdefault(canon_key, note)

    invalid: list[WatchlistImportInvalid] = []
    canon_codes: list[str] = []
    seen: set[str] = set()
    duplicate_skipped = 0
    for index, raw_code in enumerate(payload.codes):
        try:
            canon = normalize(raw_code)
        except DataError:
            invalid.append(
                WatchlistImportInvalid(
                    index=index,
                    code=raw_code,
                    reason="invalid_code",
                    message="无法识别证券代码",
                )
            )
            continue
        if canon in seen:
            duplicate_skipped += 1
            continue
        seen.add(canon)
        canon_codes.append(canon)

    existing_rows = list(
        session.execute(
            in_workspace(
                select(WatchlistItem).where(
                    WatchlistItem.list_name == list_name,
                    WatchlistItem.code.in_(canon_codes),
                ),
                WatchlistItem,
                ws.id,
            )
        )
        .scalars()
        .all()
    )
    existing = {row.code: row for row in existing_rows}
    new_codes = [code for code in canon_codes if code not in existing]

    # 只对「真正新增」的条数做一次配额检查；更新已有条目不占额度。
    quota.enforce(session, ws.id, "watchlist_items", amount=len(new_codes))

    added = 0
    updated = 0
    skipped = duplicate_skipped
    for code in canon_codes:
        row = existing.get(code)
        if row is not None:
            if payload.skip_existing:
                skipped += 1
                continue
            updated += 1
            note = notes.get(code)
            if note is not None:
                row.note = note
            continue
        session.add(
            WatchlistItem(
                workspace_id=ws.id,
                list_name=list_name,
                code=code,
                note=notes.get(code, ""),
            )
        )
        added += 1
    session.commit()

    total_items = int(
        session.execute(
            in_workspace(
                select(func.count(WatchlistItem.id)).where(WatchlistItem.list_name == list_name),
                WatchlistItem,
                ws.id,
            )
        ).scalar_one()
    )

    quota_info: WatchlistQuotaInfo | None = None
    limit = int(quota.limits()["watchlist_items"])
    if limit > 0 and ws.id > 0:
        used = int(quota.usage(session, ws.id)["watchlist_items"])
        quota_info = WatchlistQuotaInfo(
            resource="watchlist_items",
            limit=limit,
            used=used,
            remaining=max(limit - used, 0),
        )

    return WatchlistImportOut(
        list_name=list_name,
        added=added,
        updated=updated,
        skipped=skipped,
        invalid=invalid,
        list_created=payload.create_list and not existed_before and added > 0,
        total_items=total_items,
        quota=quota_info,
    )


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
