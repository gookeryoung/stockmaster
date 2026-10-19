"""工作区用量统计与可选配额。

隔离解决的是「谁能看见谁的数据」，配额解决的是「一个工作区能放多少」。两者互相
独立：

* **用量统计**永远只读、无副作用，供工作区页展示与管理员巡检；
* **配额**默认全部关闭（``0`` = 不限），只有运维显式配置 ``SM_WORKSPACE_MAX_*``
  才生效，因此不改变任何既有部署的行为；
* **隔离关闭时不执行限制**（``workspace_id <= 0`` 表示所有数据都在「未归属」桶）：
  单机模式本来就没有工作区概念，突然用工作区额度卡住写路径会破坏升级后行为一致的
  承诺。

错误用 ``HTTPException(409, X-Auth-Error: quota_exceeded)``：配额是请求级策略
（与 ``require_editor`` 同类），由路由层在写库前直接拒绝；这比抛领域异常再翻译更
直接，也不会让 CLI/脚本在无请求上下文时误触发。并发下两个请求可能同时通过检查、
短暂超出上限 1～2 条；配额是治理手段而不是精确计费，不为它引入数据库锁。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockmaster.config import settings
from stockmaster.db.models import (
    AlertRule,
    BacktestRun,
    WatchlistItem,
    WorkspaceChannel,
    WorkspaceMember,
)

__all__ = ["DATA_COUNTERS", "USAGE_COUNTERS", "USAGE_LABELS", "data_total", "data_totals", "enforce", "limits", "usage"]

#: 计入用量的资源 → ORM 模型。键同时是 API 返回与配置文件里的名字。
#: 只统计「工作区私有数据 + 成员 + 通道」，行情/指标/模型等共享参考数据不计入。
USAGE_COUNTERS: dict[str, type] = {
    "members": WorkspaceMember,
    "watchlist_items": WatchlistItem,
    "alert_rules": AlertRule,
    "backtest_runs": BacktestRun,
    "channels": WorkspaceChannel,
}

USAGE_LABELS: dict[str, str] = {
    "members": "成员",
    "watchlist_items": "自选股",
    "alert_rules": "预警规则",
    "backtest_runs": "回测记录",
    "channels": "通知通道",
}

#: 「数据量」口径：只算工作区里的业务数据，不含成员与通知通道。
#:
#: 用户页/管理员列表展示的「数据量」用这个口径；与用量卡片的 ``total``（含成员、
#: 通道）刻意区分开 —— 把「成员 1 人」算进「数据 1 条」在列表里很怪。
DATA_COUNTERS: dict[str, type] = {
    key: model for key, model in USAGE_COUNTERS.items() if key not in ("members", "channels")
}


def limits() -> dict[str, int]:
    """Current per-workspace limits (``0`` = unlimited)."""
    return {
        "members": int(settings.workspace_max_members),
        "watchlist_items": int(settings.workspace_max_watchlist_items),
        "alert_rules": int(settings.workspace_max_alert_rules),
        "backtest_runs": int(settings.workspace_max_backtest_runs),
        "channels": int(settings.workspace_max_channels),
    }


def _count(session: Session, model: type, workspace_id: int) -> int:
    return int(
        session.execute(select(func.count()).select_from(model).where(model.workspace_id == workspace_id)).scalar_one()
    )


def usage(session: Session, workspace_id: int) -> dict[str, int]:
    """该工作区在各资源上的当前用量（只读）。"""
    return {resource: _count(session, model, workspace_id) for resource, model in USAGE_COUNTERS.items()}


def data_total(session: Session, workspace_id: int) -> int:
    """单个工作区的「数据量」：自选/规则/回测，不含成员与通道。"""
    return sum(_count(session, model, workspace_id) for model in DATA_COUNTERS.values())


def data_totals(session: Session, workspace_ids) -> dict[int, int]:
    """批量计算多个工作区的数据量，供工作区列表/管理员用户列表避免 N×M 次查询。

    按表各做一次 ``GROUP BY workspace_id``；输入 id 去重、缺失的 id 补 0，调用方
    不需要自己兜底。
    """
    ids = list(dict.fromkeys(int(i) for i in workspace_ids))
    totals = dict.fromkeys(ids, 0)
    if not ids:
        return totals
    for model in DATA_COUNTERS.values():
        rows = session.execute(
            select(model.workspace_id, func.count()).where(model.workspace_id.in_(ids)).group_by(model.workspace_id)
        ).all()
        for workspace_id, count in rows:
            totals[int(workspace_id)] += int(count)
    return totals


def enforce(session: Session, workspace_id: int, resource: str, *, amount: int = 1) -> None:
    """写路径调用：超限时抛 409，不写任何行。

    ``resource`` 必须是 :data:`USAGE_COUNTERS` 的键（未知键说明代码写错，直接
    ``KeyError`` 比静默放行安全）。
    """
    if amount <= 0:
        return
    limit = limits()[resource]
    # 隔离关闭 / 未归属桶：没有工作区语义，保持升级前的行为（不限制）。
    if limit <= 0 or workspace_id <= 0:
        return
    model = USAGE_COUNTERS[resource]
    current = _count(session, model, workspace_id)
    if current + amount <= limit:
        return
    label = USAGE_LABELS[resource]
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"工作区{label}已达上限（{limit} 条），请先清理或联系管理员调整配额",
        headers={"X-Auth-Error": "quota_exceeded"},
    )
