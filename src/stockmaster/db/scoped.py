"""按工作区过滤查询的通用助手。

为什么不是「每个端点自己写 ``where(workspace_id == ...)``」
------------------------------------------------------
隔离最容易出的漏子不是写错条件，而是**漏写**某一条 —— 尤其是后来新增的端点。
把判断收进一个函数之后，漏写的表现是「调不到助手」而不是「静默返回别人的数据」。

为什么不是 403
--------------
不属于当前工作区的资源一律返回 **404**。403 等于确认「这个 id 存在，只是不给你
看」，攻击者可以靠状态码差异枚举出某个工作区有多少条规则、回测叫什么名字。

开关语义
--------
``SM_DATA_ISOLATION=false``（默认）时所有过滤退化为恒真，行为与隔离前逐字节一致 ——
这是能安全灰度、也能一键回退的前提。列始终在表里，回退不需要动 schema。

与角色判定的分工
----------------
这里只管「看得见什么」；「能不能改」由 ``stockmaster.core.workspace_deps.require_editor`` 负责。
两者分开的原因：同一批数据对不同角色的可见性相同，只有写权限不同。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import Select
from sqlalchemy.orm import Session

from stockmaster.config import settings
from stockmaster.db.models import WORKSPACE_UNASSIGNED

# 未归属数据的归属值（单机模式、以及隔离上线前的历史数据）。
GUEST_WORKSPACE_ID = WORKSPACE_UNASSIGNED


def scope_enabled() -> bool:
    """隔离是否生效。关掉时下面所有助手都变成空操作。"""
    return settings.data_isolation


def in_workspace(stmt: Select, model: type, workspace_id: int) -> Select:
    """给查询加上工作区条件（隔离关闭时原样返回）。"""
    if not scope_enabled():
        return stmt
    return stmt.where(model.workspace_id == workspace_id)


def belongs_to(row, workspace_id: int) -> bool:
    """这一行是否属于该工作区（隔离关闭时恒真）。"""
    if row is None:
        return False
    if not scope_enabled():
        return True
    return getattr(row, "workspace_id", GUEST_WORKSPACE_ID) == workspace_id


def get_in_workspace_or_404[T](
    session: Session,
    model: type[T],
    ident: object,
    workspace_id: int,
    label: str,
) -> T:
    """按主键取一行，不存在或不属于当前工作区时统一 404。"""
    row = session.get(model, ident)
    if row is None or not belongs_to(row, workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} {ident} not found",
        )
    return row


__all__ = [
    "GUEST_WORKSPACE_ID",
    "belongs_to",
    "get_in_workspace_or_404",
    "in_workspace",
    "scope_enabled",
]
