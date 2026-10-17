"""工作区级预警参数覆盖：读取、校验与「只收紧」有效值计算。

设计约束见 ``db/models.py`` 的 ``WorkspaceAlertPolicy``：策略只能把规则变得更严格，
不能放宽。所有调用方（调度器扫描、手动扫描、API 展示）都通过这里的函数拿有效值，
避免同一条语义在多个地方各写一遍。

无行 / 字段为 NULL = 跟随规则。``workspace_id <= 0``（隔离关闭的未归属桶）永远按
默认值处理，与引入工作区前行为一致。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.db.models import WorkspaceAlertPolicy

__all__ = [
    "DEFAULT_POLICY",
    "EffectivePolicy",
    "effective_policies",
    "effective_policy",
    "policies_for",
    "policy_for",
]


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """有效值（NULL 已归一）：多个维度取更严格者时使用。"""

    exclude_st: bool = False
    min_amount: float = 0.0
    cooldown_seconds: int = 0
    once_per_day: bool = False


DEFAULT_POLICY = EffectivePolicy()


def policy_for(session: Session, workspace_id: int) -> WorkspaceAlertPolicy | None:
    """工作区的策略行；不存在返回 None（全部跟随规则）。"""
    if workspace_id <= 0:
        return None
    return session.get(WorkspaceAlertPolicy, workspace_id)


def policies_for(session: Session, workspace_ids: Iterable[int]) -> dict[int, WorkspaceAlertPolicy]:
    """批量取策略行，供一次扫描中按工作区 O(1) 查询。"""
    ids = [int(workspace_id) for workspace_id in dict.fromkeys(workspace_ids) if workspace_id > 0]
    if not ids:
        return {}
    rows = session.execute(select(WorkspaceAlertPolicy).where(WorkspaceAlertPolicy.workspace_id.in_(ids))).scalars()
    return {int(row.workspace_id): row for row in rows}


def effective_policy(row: WorkspaceAlertPolicy | None) -> EffectivePolicy:
    """把策略行归一成有效值；None 则返回默认（全部不覆盖）。"""
    if row is None:
        return DEFAULT_POLICY
    return EffectivePolicy(
        exclude_st=bool(row.exclude_st),
        min_amount=max(0.0, float(row.min_amount or 0.0)),
        cooldown_seconds=max(0, int(row.cooldown_seconds or 0)),
        once_per_day=bool(row.once_per_day),
    )


def effective_policies(session: Session, workspace_ids: Iterable[int]) -> dict[int, EffectivePolicy]:
    """批量版 ``effective_policy``；未配置的工作区不出现在结果里（调用方用默认值兜底）。"""
    return {workspace_id: effective_policy(row) for workspace_id, row in policies_for(session, workspace_ids).items()}
