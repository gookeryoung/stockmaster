"""工作区级策略配置：选股 / 样本 / 算法三张私有表。

背景
----
工作区隔离（``0003``）把「自选、预警、回测历史」变成了工作区共有的数据，但用户
自定义的**方法论**仍停留在一次性表单里：每次训练都要重填算法与超参，每次回测都要
重选股票池，多人协作时无法确认「用的到底是不是同一套参数」。本迁移把三类配置
落成命名资源：

* ``screening_configs`` 选股配置（筛选条件 + 排序 + 条数上限）；
* ``sample_configs`` 样本配置（标的池来源 + 日期窗口 + 预测周期）；
* ``algorithm_configs`` 算法配置（算法 + 超参 + 绑定的样本）。

它们与既有四张私有表同构：``workspace_id`` 决定归属（``0`` = 未归属桶），工作区内
``name`` 唯一。这样开/关 ``SM_DATA_ISOLATION`` 都不需要再动 schema。

幂等性
------
``init_db()`` 的顺序是 ``create_all`` → Alembic 桥接：全新库的三张表已经由 ORM
元数据建好，走到本迁移时必须跳过；存量库（停在 0003）则可能由 ``create_all`` 或
``alembic upgrade`` 任一通道补齐。所以每个 ``create_table`` 前都先查表是否存在。

Revision ID: 0004_strategy_configs
Revises: 0003_workspaces
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_strategy_configs"
down_revision: str | None = "0003_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in set(_inspector().get_table_names())


def _create_screening_configs() -> None:
    if _has_table("screening_configs"):
        return
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


def _create_sample_configs() -> None:
    if _has_table("sample_configs"):
        return
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


def _create_algorithm_configs() -> None:
    if _has_table("algorithm_configs"):
        return
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


def upgrade() -> None:
    # 顺序有依赖：algorithm_configs.sample_id 引用 sample_configs。
    _create_screening_configs()
    _create_sample_configs()
    _create_algorithm_configs()


def downgrade() -> None:
    """删表即可：配置是纯用户数据，没有下游表引用。"""
    for table in ("algorithm_configs", "sample_configs", "screening_configs"):
        if _has_table(table):
            op.drop_table(table)
