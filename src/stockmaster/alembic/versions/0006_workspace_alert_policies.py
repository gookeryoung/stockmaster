"""工作区级预警参数覆盖表。

背景
----
工作区隔离之后，预警规则属于工作区，但「团队层面的提醒底线」无处可配：owner 想让
本工作区所有提醒都排除 ST、成交额低于某阈值不提醒、冷却至少 N 秒，只能逐条规则改，
既容易漏又容易被后来新增的规则绕过。本迁移建 ``workspace_alert_policies``（一个
工作区最多一行），字段为 ``NULL`` 表示该维度跟随规则；有效值按「规则与策略取更严格
者」计算（见 ``plugins/alerts/services/policy.py``），策略只能收紧、不能放宽。

表结构要点
----------
* ``workspace_id`` 既是主键也是外键（``ON DELETE CASCADE``）：一工作区一行，删除
  工作区时随之清理；不存在 ``workspace_id=0`` 的策略行。
* 所有覆盖字段可为 NULL；只有用户显式配置的维度才会写入非空值。

幂等性
------
与 ``0004`` / ``0005`` 相同：``init_db()`` 先 ``create_all``，全新库的表已由 ORM 元
数据建好，迁移必须跳过；存量库（停在 0005）才真正建表。所以建表前先探测。

Revision ID: 0006_workspace_alert_policies
Revises: 0005_workspace_channels
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_workspace_alert_policies"
down_revision: str | None = "0005_workspace_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table: str) -> bool:
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _has_table("workspace_alert_policies"):
        return
    op.create_table(
        "workspace_alert_policies",
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("exclude_st", sa.Boolean(), nullable=True),
        sa.Column("min_amount", sa.Float(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("once_per_day", sa.Boolean(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )


def downgrade() -> None:
    """删表即可：策略是纯配置，没有下游表引用。"""
    if _has_table("workspace_alert_policies"):
        op.drop_table("workspace_alert_policies")
