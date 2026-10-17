"""工作区用量趋势与导出端点。

与 ``routers/workspaces.py`` 分开的原因
-------------------------------------
那个文件已经承载工作区 CRUD、成员、通道、预警策略；趋势与导出是只读的统计视图，
单独成文件后 ``plugin.py`` 只需把两个 router 组合起来，既有端点一行都不用动。

权限
----
两个端点都走 ``path_workspace``：任何成员可读（导出的是该工作区自己的用量，
不包含密钥或他人数据），非成员与不存在的 id 一律 404。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from stockmaster.config import settings
from stockmaster.core import quota
from stockmaster.core.deps import db_session
from stockmaster.core.workspace_deps import WorkspaceCtx, path_workspace
from stockmaster.plugins.workspaces.schemas.usage import WorkspaceUsagePointOut
from stockmaster.plugins.workspaces.services import usage_history

router = APIRouter(tags=["workspaces"])


@router.get("/{workspace_id}/stats/export")
def export_workspace_usage(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)],
    session: Annotated[Session, Depends(db_session)],
) -> Response:
    """导出工作区用量与配额的 CSV（成员可读）。

    UTF-8 BOM 是为了让 Excel 直接双击打开时按 UTF-8 解析中文；文件头带工作区名、
    id 与导出时间，方便把多份导出放在一起时辨认来源。
    """
    current = quota.usage(session, ctx.id)
    caps = quota.limits()
    labels = quota.USAGE_LABELS
    exported_at = datetime.now(settings.tz)
    workspace_name = ctx.workspace.name if ctx.workspace is not None else ""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["工作区", workspace_name])
    writer.writerow(["工作区 ID", ctx.id])
    writer.writerow(["导出时间", exported_at.strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow([])
    writer.writerow(["资源", "名称", "用量", "上限"])
    for resource in quota.USAGE_COUNTERS:
        limit = caps.get(resource, 0)
        # 与页面展示保持一致：0 = 不限，导出的表格也不能显示成「0 条上限」。
        writer.writerow(
            [resource, labels.get(resource, resource), current.get(resource, 0), "不限" if limit <= 0 else limit]
        )
    writer.writerow([])
    writer.writerow(["合计", "", sum(current.values()), ""])

    filename = f"workspace-{ctx.id}-usage-{exported_at:%Y%m%d}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{workspace_id}/stats/history", response_model=list[WorkspaceUsagePointOut])
def workspace_usage_history(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)],
    session: Annotated[Session, Depends(db_session)],
    days: Annotated[int, Query(ge=1, le=365, description="回溯天数（含最新一天）")] = 30,
) -> list[WorkspaceUsagePointOut]:
    """用量趋势点（成员可读，按日期升序；没有快照时返回空列表）。"""
    return [WorkspaceUsagePointOut.model_validate(row) for row in usage_history.history(session, ctx.id, days=days)]
