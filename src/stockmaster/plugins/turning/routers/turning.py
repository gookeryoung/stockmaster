"""turning 插件路由：顶底识别状态扫描与历史事件。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from stockmaster.core.deps import canonical_code, db_session, require_instrument
from stockmaster.plugins.turning.schemas.turning import (
    TurningEvent,
    TurningHistoryResponse,
    TurningPercentiles,
    TurningRulesResponse,
    TurningScanResponse,
    TurningStatusRow,
)
from stockmaster.plugins.turning.services import tp_service

router = APIRouter(tags=["turning"])


@router.get("/turning/scan", response_model=TurningScanResponse)
def scan(
    codes: str = Query(description="逗号分隔的证券代码，最多 50 个"),
    thr: float = Query(default=75.0, ge=50.0, le=95.0, description="底部分位阈值"),
    confirm: int = Query(default=3, ge=0, le=10, description="右侧确认窗口（交易日）"),
    session: Session = Depends(db_session),
):
    """对一组标的计算最新顶底状态（七维分位 + 最短板/共振计数 + 闸门 + 信号）。

    请求池 ≥3 只时启用池内等权板块闸门；缺失或历史不足的代码进入 ``missing``。
    """
    try:
        code_list = tp_service.validate_codes(codes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
            headers={"X-Auth-Error": "invalid_codes"},
        ) from exc

    items, missing = tp_service.scan_codes(session, code_list, {"thr": thr, "confirm": confirm})
    rows = [
        TurningStatusRow(
            code=item["code"],
            name=_instrument_name(session, item["code"]),
            trade_date=item["trade_date"] or None,
            close=item["close"],
            bottom=item["bottom"],
            top_count=item["top_count"],
            gate=item["gate"],
            hold_thr=item["hold_thr"],
            candidate=item["candidate"],
            entry=item["entry"],
            percentiles=TurningPercentiles(**item["percentiles"]),
        )
        for item in items
    ]
    return TurningScanResponse(
        trade_date=tp_service.latest_trade_date(items),
        items=rows,
        missing=missing,
    )


@router.get("/turning/history/{code}", response_model=TurningHistoryResponse)
def history(
    code: str,
    kind: str = Query(default="bottom", pattern="^(bottom|top)$"),
    limit: int = Query(default=50, ge=1, le=200),
    thr: float = Query(default=75.0, ge=50.0, le=95.0),
    session: Session = Depends(db_session),
):
    """一只标的的历史顶底事件（冷却去重，只看事件首日）。"""
    canon = canonical_code(code)
    name = _instrument_name(session, canon)
    try:
        events = tp_service.history_events(session, canon, kind, {"thr": thr}, limit)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return TurningHistoryResponse(
        code=canon,
        name=name,
        kind=kind,
        events=[TurningEvent(**event) for event in events],
    )


@router.get("/turning/rules", response_model=TurningRulesResponse)
def rules():
    """模型的买卖口径说明（与 core/tpcore.describe 一致，供前端展示）。"""
    from stockmaster.core.tpcore import describe

    d = describe()
    return TurningRulesResponse(buy=d["buy"], sell=d["sell"], note=d["note"])


def _instrument_name(session: Session, code: str) -> str:
    try:
        instrument = require_instrument(session, code)
    except Exception:
        return ""
    return instrument.name or ""
