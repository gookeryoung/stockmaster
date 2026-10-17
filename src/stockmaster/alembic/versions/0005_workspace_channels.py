"""工作区级通知通道：把预警外发从「全站一个 webhook」改成「每个工作区自己的通道」。

背景
----
``0003_workspaces`` 之后，自选、规则、触发记录都按工作区隔离了，但通知出口仍是
``SM_ALERT_WEBHOOK_URL`` 这一个环境变量：两个工作区的规则同时触发时，提醒会混进
同一个群机器人，A 组能看到 B 组的股票与阈值。本迁移建 ``workspace_channels``，
让每个工作区（owner）配置自己的 webhook / Bark 通道；全局 webhook 只保留给
``workspace_id=0`` 的未归属数据，或显式开启 ``SM_ALERT_GLOBAL_FALLBACK`` 时兜底。

表结构要点
----------
* ``workspace_id`` 有外键（``ON DELETE CASCADE``）：通道是工作区配置，删除工作区
  时随之清理，不会留下没有归属的投递目标；因此也**不会**出现 ``workspace_id=0``
  的通道行。
* ``(workspace_id, name)`` 唯一：名字是成员识别「这条发到哪」的入口。
* ``url`` 存明文（与全局 webhook 同级的信任边界），但 API 只返回掩码。

幂等性
------
与 ``0004`` 相同：```init_db()`` 走 ``create_all`` → Alembic 桥接，全新库的表已由
ORM 元数据建好，迁移必须跳过；存量库（停在 0004）才真正建表。所以建表前先探测。

Revision ID: 0005_workspace_channels
Revises: 0004_strategy_configs
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_workspace_channels"
down_revision: str | None = "0004_strategy_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table: str) -> bool:
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _has_table("workspace_channels"):
        return
    op.create_table(
        "workspace_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="webhook"),
        sa.Column("url", sa.String(512), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_workspace_channel_name"),
    )
    op.create_index("ix_workspace_channels_workspace", "workspace_channels", ["workspace_id", "enabled"])


def downgrade() -> None:
    """删表即可：通道是外发配置，没有下游表引用它。"""
    if _has_table("workspace_channels"):
        op.drop_table("workspace_channels")
