"""按归属（``owner_id``）过滤查询的通用助手。

为什么不是「每个端点自己写 ``where(owner_id == ...)``」
--------------------------------------------------
隔离最容易出的漏子不是写错条件，而是**漏写**某一条 —— 尤其是后来新增的端点。
把判断收进一个函数之后，漏写的表现是「调不到助手」而不是「静默返回别人的数据」。

为什么不是 403
--------------
非本人资源一律返回 **404**。403 等于确认「这个 id 存在，只是不给你看」，攻击者可以
靠状态码差异枚举出别人有多少条规则、回测叫什么名字。

开关语义
--------
``SM_DATA_ISOLATION=false``（默认）时所有过滤退化为恒真，行为与隔离前逐字节一致 ——
这是能安全灰度、也能一键回退的前提。列始终在表里，回退不需要动 schema。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import Select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OWNER_UNASSIGNED

# 未归属数据的归属值（单机模式、以及隔离上线前的历史数据）。
GUEST_OWNER_ID = OWNER_UNASSIGNED


def scope_enabled() -> bool:
    """隔离是否生效。关掉时下面所有助手都变成空操作。"""
    return settings.data_isolation


def owned(stmt: Select, model: type, owner: int) -> Select:
    """给查询加上归属条件（隔离关闭时原样返回）。"""
    if not scope_enabled():
        return stmt
    return stmt.where(model.owner_id == owner)


def owns(row, owner: int) -> bool:
    """这一行是否归 ``owner`` 所有（隔离关闭时恒真）。"""
    if row is None:
        return False
    if not scope_enabled():
        return True
    return getattr(row, "owner_id", GUEST_OWNER_ID) == owner


def get_owned_or_404[T](
    session: Session,
    model: type[T],
    ident: object,
    owner: int,
    label: str,
) -> T:
    """按主键取一行，不存在或不属于本人时统一 404。"""
    row = session.get(model, ident)
    if row is None or not owns(row, owner):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} {ident} not found",
        )
    return row


__all__ = ["GUEST_OWNER_ID", "get_owned_or_404", "owned", "owns", "scope_enabled"]
