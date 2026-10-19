"""工作区用量趋势快照：每日写一行，供趋势图与导出使用。

为什么落表而不是实时聚合
------------------------
``quota.usage()`` 只回答「现在是多少」，没有历史就画不出趋势。每日收盘后把结果
固化成一行的成本极低（一个工作区一行），却能让「这个工作区是不是在膨胀」一眼可见。

幂等性
------
``(workspace_id, captured_on)`` 唯一。调度任务可能因为手工触发、补跑或重启而
在同一天执行多次，这里用「先查后写」而不是 INSERT：重复执行只更新当天那一行，
既不会产生重复点，也不会因为唯一约束报错。
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockmaster.core import quota
from stockmaster.db import session_scope
from stockmaster.db.models import Workspace, WorkspaceUsageSnapshot

__all__ = ["capture_all", "capture_snapshot", "history"]


def capture_snapshot(session: Session, workspace_id: int, *, on: date | None = None) -> WorkspaceUsageSnapshot:
    """记录（或更新）某工作区某一天的用量快照。

    计算口径与 ``GET /workspaces/{id}/stats`` 完全一致（同一个 ``quota`` 模块），
    避免「页面上显示 10 条、趋势图显示 8 条」这种对不上账的情况。调用方负责提交
    事务（``capture_all`` 用 ``session_scope``，测试可直接包一层事务）。
    """
    day = on or date.today()
    current = quota.usage(session, workspace_id)

    row = session.execute(
        select(WorkspaceUsageSnapshot).where(
            WorkspaceUsageSnapshot.workspace_id == workspace_id,
            WorkspaceUsageSnapshot.captured_on == day,
        )
    ).scalar_one_or_none()
    if row is None:
        row = WorkspaceUsageSnapshot(workspace_id=workspace_id, captured_on=day)
        session.add(row)

    row.data_total = quota.data_total(session, workspace_id)
    row.member_count = current["members"]
    row.watchlist_items = current["watchlist_items"]
    row.alert_rules = current["alert_rules"]
    row.backtest_runs = current["backtest_runs"]
    session.flush()
    return row


def capture_all() -> dict:
    """给每个真实工作区（``id > 0``）写一条今天的快照。

    取全部工作区而不是「当前请求的工作区」，因为这是收盘后的全局任务；``id = 0``
    是未归属历史数据桶，不是真实工作区，没有趋势可画。
    """
    day = date.today()
    with session_scope() as session:
        workspace_ids = list(
            session.execute(select(Workspace.id).where(Workspace.id > 0).order_by(Workspace.id)).scalars().all()
        )
        for workspace_id in workspace_ids:
            capture_snapshot(session, int(workspace_id), on=day)
    return {"job": "workspace_usage", "workspaces": len(workspace_ids), "captured_on": day.isoformat()}


def history(session: Session, workspace_id: int, *, days: int = 30) -> list[WorkspaceUsageSnapshot]:
    """最近 ``days`` 天的快照，按日期升序。

    窗口以**该工作区最新一条快照**为基准向前回溯，而不是机械地用「今天」做锚点：
    调度停摆几天再恢复时，图上仍能看到最近积累的点，而不是一片空白；``days`` 是
    含最新当天在内的天数（``days=1`` 只返回最新一天）。
    """
    latest = session.execute(
        select(func.max(WorkspaceUsageSnapshot.captured_on)).where(WorkspaceUsageSnapshot.workspace_id == workspace_id)
    ).scalar()
    if latest is None:
        return []
    start = latest - timedelta(days=max(1, days) - 1)
    rows = (
        session.execute(
            select(WorkspaceUsageSnapshot)
            .where(
                WorkspaceUsageSnapshot.workspace_id == workspace_id,
                WorkspaceUsageSnapshot.captured_on >= start,
                WorkspaceUsageSnapshot.captured_on <= latest,
            )
            .order_by(WorkspaceUsageSnapshot.captured_on)
        )
        .scalars()
        .all()
    )
    return list(rows)
