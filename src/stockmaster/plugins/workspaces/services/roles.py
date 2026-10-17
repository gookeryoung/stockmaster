"""工作区角色：``owner`` > ``editor`` > ``viewer``。

为什么是三级而不是权限位
--------------------
cndb 的表级权限用的是 ``{action_key: bool}`` 位图，适合「同一张表上要做十几种
动作」的场景。StockMaster 的工作区里只有三类动作 —— 读数据、写数据、管成员 ——
用等级比较（``ROLE_RANK[role] >= ROLE_RANK[minimum]``）表达更不容易写错：新加
端点时只需声明「至少要 editor」，不用逐个核对权限位。

等级语义
--------
=====  ====  ==========================================================
角色    等级  能做什么
=====  ====  ==========================================================
owner    2   数据读写 + 改工作区信息 + 增删成员 / 改角色 / 转让所有权
editor   1   数据读写（自选、预警规则、回测）
viewer   0   只读；任何写操作返回 403
=====  ====  ==========================================================

``can_write`` 与 ``can_manage`` 刻意做成函数而不是散落的字符串比较：判断「这个人
能不能写」的地方有十几处，任何一处写成 ``role != "viewer"`` 都会在角色扩展时出错。
"""

from __future__ import annotations

from enum import StrEnum


class WorkspaceRole(StrEnum):
    """工作区成员角色，权限从高到低。"""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


ROLE_RANK: dict[str, int] = {
    WorkspaceRole.OWNER: 2,
    WorkspaceRole.EDITOR: 1,
    WorkspaceRole.VIEWER: 0,
}

ROLE_LABELS: dict[str, str] = {
    WorkspaceRole.OWNER: "所有者",
    WorkspaceRole.EDITOR: "编辑者",
    WorkspaceRole.VIEWER: "查看者",
}

# 成员管理里可以直接授予的角色。owner 只能通过「转让所有权」得到 —— 直接授予会让
# 「谁是所有者」变成多个人并存，最后一个 owner 的保护也就失效了。
GRANTABLE_ROLES: tuple[str, ...] = (WorkspaceRole.EDITOR, WorkspaceRole.VIEWER)

ALL_ROLES: tuple[str, ...] = (WorkspaceRole.OWNER, WorkspaceRole.EDITOR, WorkspaceRole.VIEWER)


def normalize_role(value: str | None) -> str:
    """把任意大小写/枚举名的写法收敛成标准值；无法识别时按最低权限处理。"""
    text = (value or "").strip().lower()
    return text if text in ROLE_RANK else WorkspaceRole.VIEWER


def is_valid_role(value: str | None) -> bool:
    return (value or "").strip().lower() in ROLE_RANK


def rank(role: str | None) -> int:
    """角色等级。未知角色返回 -1，使任何 ``>=`` 比较都失败（拒绝优先）。"""
    return ROLE_RANK.get(normalize_role(role), -1)


def at_least(role: str | None, minimum: str | WorkspaceRole) -> bool:
    """角色是否达到 ``minimum``。

    未知角色一律返回 False，而不是退化成 viewer 再参与比较 —— 退化的结果会让
    ``at_least("superuser", "viewer")`` 为真，等于「拼错角色名反而拿到最低权限」，
    这类默认放行比报错危险得多。
    """
    if not is_valid_role(role):
        return False
    return ROLE_RANK[normalize_role(role)] >= ROLE_RANK[normalize_role(str(minimum))]


def can_write(role: str | None) -> bool:
    """能否写工作区数据（viewer 不能）。"""
    return at_least(role, WorkspaceRole.EDITOR)


def can_manage(role: str | None) -> bool:
    """能否管理工作区本身与成员（仅 owner）。"""
    return at_least(role, WorkspaceRole.OWNER)


def label(role: str | None) -> str:
    return ROLE_LABELS.get(normalize_role(role), "未知")


__all__ = [
    "ALL_ROLES",
    "GRANTABLE_ROLES",
    "ROLE_LABELS",
    "ROLE_RANK",
    "WorkspaceRole",
    "at_least",
    "can_manage",
    "can_write",
    "is_valid_role",
    "label",
    "normalize_role",
    "rank",
]
