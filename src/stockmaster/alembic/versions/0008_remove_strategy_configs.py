"""移除工作区级策略配置：选股 / 样本 / 算法三张私有表。

背景
----
``0004`` 引入的三类命名配置（``screening_configs`` / ``sample_configs`` /
``algorithm_configs``）对应的「策略」功能整体下线。ORM 模型、插件、配额统计与
前端页面均已删除，本迁移负责清理存量库中的物理表与遗留列，保持 schema 与代码
一致。

幂等性
------
与 ``0004``～``0007`` 相同约定：``init_db()`` 的顺序是 ``create_all`` → Alembic
桥接。全新库已不再由 ORM 元数据建这三张表，走到本迁移时它们本就不存在，必须跳过；
存量库（停在 0007）才真正执行删除。所以删表前查表是否存在、删列前查列是否存在。

顺序有依赖：``algorithm_configs.sample_id`` 引用 ``sample_configs``，先删引用方。

Revision ID: 0008_remove_strategy_configs
Revises: 0007_workspace_email_and_usage_history
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_remove_strategy_configs"
down_revision: str | None = "0007_workspace_email_and_usage_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in set(_inspector().get_table_names())


def _has_column(table: str, column: str) -> bool:
    return column in {item["name"] for item in _inspector().get_columns(table)}


def upgrade() -> None:
    # algorithm_configs 依赖 sample_configs，先删引用方。
    for table in ("algorithm_configs", "sample_configs", "screening_configs"):
        if _has_table(table):
            op.drop_table(table)

    if _has_table("workspace_usage_snapshots") and _has_column(
        "workspace_usage_snapshots", "config_total"
    ):
        with op.batch_alter_table("workspace_usage_snapshots") as batch:
            batch.drop_column("config_total")


def downgrade() -> None:
    """重建三张表与 config_total 列，结构对齐 ``0004`` / ``0007``。"""
    if not _has_table("screening_configs"):
        op.create_table(
            "screening_configs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.String(256), nullable=False, server_default=""),
            sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("workspace_id", "name", name="uq_screening_config_name"),
        )
        op.create_index("ix_screening_configs_workspace", "screening_configs", ["workspace_id"])

    if not _has_table("sample_configs"):
        op.create_table(
            "sample_configs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.String(256), nullable=False, server_default=""),
            sa.Column("universe_type", sa.String(16), nullable=False, server_default="default"),
            sa.Column("universe_value", sa.String(512), nullable=False, server_default=""),
            sa.Column("start_date", sa.Date(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("lookback_days", sa.Integer(), nullable=True),
            sa.Column("horizon", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("min_bars", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("workspace_id", "name", name="uq_sample_config_name"),
        )
        op.create_index("ix_sample_configs_workspace", "sample_configs", ["workspace_id"])

    if not _has_table("algorithm_configs"):
        op.create_table(
            "algorithm_configs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.String(256), nullable=False, server_default=""),
            sa.Column("algo", sa.String(32), nullable=False, server_default="hist_gbdt"),
            sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "sample_id",
                sa.Integer(),
                sa.ForeignKey("sample_configs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("horizon", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("test_fraction", sa.Float(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("workspace_id", "name", name="uq_algorithm_config_name"),
        )
        op.create_index("ix_algorithm_configs_workspace", "algorithm_configs", ["workspace_id"])

    if _has_table("workspace_usage_snapshots") and not _has_column(
        "workspace_usage_snapshots", "config_total"
    ):
        with op.batch_alter_table("workspace_usage_snapshots") as batch:
            batch.add_column(
                sa.Column("config_total", sa.Integer(), nullable=False, server_default="0")
            )
