"""P3 可选增强：邮件通知通道、按工作区消息模板、用量趋势快照。

背景
----
隔离路线图（PR-1～PR-7）完成后，剩下的是「非隔离核心」的体验增强：

* **邮件通知**——已有 webhook/Bark，但很多团队只用邮件；SMTP 是部署级凭据，
  收件人则按工作区通道配置（``workspace_channels.kind='email'``，收件人写在 ``url``
  列，多个用逗号分隔）。本迁移不改通道表结构，只加工作区级消息模板列。
* **按工作区消息模板**——``workspaces.message_template``，空 = 默认 title/body，
  支持 ``{title} {body} {code} {rule_id} {level}`` 占位符。
* **用量趋势**——``workspace_usage_snapshots`` 每日快照（一行/工作区/天，唯一约束），
  由调度任务在收盘后写入，前端画趋势图并支持 CSV 导出。

幂等性
------
与 ``0004``～``0006`` 相同：``init_db()`` 先 ``create_all``，全新库的表/列已由 ORM 元
数据建好，迁移必须跳过；存量库（停在 0006）才真正执行增量。所以加列前检查列是否已
存在、建表前检查表是否存在。

Revision ID: 0007_workspace_email_and_usage_history
Revises: 0006_workspace_alert_policies
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_workspace_email_and_usage_history"
down_revision: str | None = "0006_workspace_alert_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in set(_inspector().get_table_names())


def _has_column(table: str, column: str) -> bool:
    return column in {item["name"] for item in _inspector().get_columns(table)}


def upgrade() -> None:
    if not _has_column("workspaces", "message_template"):
        op.add_column(
            "workspaces",
            sa.Column("message_template", sa.Text(), nullable=False, server_default=""),
        )

    if not _has_table("workspace_usage_snapshots"):
        op.create_table(
            "workspace_usage_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "workspace_id",
                sa.Integer(),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("captured_on", sa.Date(), nullable=False),
            sa.Column("data_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("watchlist_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("alert_rules", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("backtest_runs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("config_total", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("workspace_id", "captured_on", name="uq_workspace_usage_snapshot_day"),
        )
        op.create_index(
            "ix_workspace_usage_snapshots_workspace",
            "workspace_usage_snapshots",
            ["workspace_id", "captured_on"],
        )


def downgrade() -> None:
    """只回退 P3 新增物：快照表与工作区模板列。"""
    if _has_table("workspace_usage_snapshots"):
        op.drop_table("workspace_usage_snapshots")
    if _has_column("workspaces", "message_template"):
        with op.batch_alter_table("workspaces") as batch:
            batch.drop_column("message_template")
