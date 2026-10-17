"""给用户私有数据加上 ``owner_id``，并把自选股唯一约束改为含归属。

为什么是「增量」而不是重建表
--------------------------
schema 的真源是 ORM 模型，``init_db()`` 里 ``create_all`` 已经先跑过一遍。于是本
迁移在**全新库**上会看到列已存在，在**存量库**上则要真的加。两种情形都要能跑通，
所以每一步都先探测再执行（幂等）。这和「迁移链只记录增量、且必须能对存量库生效」
的约定是一致的 —— 存量库认领时先 stamp 到 baseline 再 upgrade head，靠的就是这里
的幂等性。

唯一约束为什么必须换
------------------
旧约束是 ``(list_name, code)``，语义是「全局一个 default 清单」。两个用户各加同一
只股票到 ``default`` 时，第二个人会命中冲突分支，把第一个人的 ``note`` 覆盖掉 ——
数据串台且不报错。加上 ``owner_id`` 前缀之后两者互不干扰。

``render_as_batch`` 已在 ``env.py`` 打开：SQLite 没有 ``DROP CONSTRAINT``，batch 模式
会建新表 → 搬数据 → 换名，这是唯一可行的路径。

Revision ID: 0002_owner_columns
Revises: 0001_baseline
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_owner_columns"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 只有「用户私有数据」加归属列。行情、因子、模型、任务这类共享数据不加 ——
# 加了会让每个用户各存一份同样的 K 线，磁盘和同步时间都翻倍。
OWNED_TABLES = ("watchlist_items", "alert_rules", "alert_triggers", "backtest_runs")

WATCHLIST_UQ = "uq_watchlist_item"
WATCHLIST_UQ_COLUMNS = ["owner_id", "list_name", "code"]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _unique_constraint(table: str, name: str) -> list[str] | None:
    """已存在的同名唯一约束的列；不存在返回 ``None``。"""
    for row in _inspector().get_unique_constraints(table):
        if row.get("name") == name:
            return list(row.get("column_names") or [])
    return None


def upgrade() -> None:
    for table in OWNED_TABLES:
        if _has_column(table, "owner_id"):
            continue
        # ``server_default`` 让历史行一次性拿到 0，也保护「老客户端插入不带该列」。
        # 0 不是合法用户 id，含义是「尚未归属」，单机模式会一直用它。
        op.add_column(
            table,
            sa.Column("owner_id", sa.Integer(), nullable=False, server_default="0"),
        )

    existing = _unique_constraint("watchlist_items", WATCHLIST_UQ)
    if existing != WATCHLIST_UQ_COLUMNS:
        with op.batch_alter_table("watchlist_items", schema=None) as batch:
            if existing is not None:
                batch.drop_constraint(WATCHLIST_UQ, type_="unique")
            batch.create_unique_constraint(WATCHLIST_UQ, WATCHLIST_UQ_COLUMNS)

    # 触发记录冗余 owner 是为了让历史页不 join 回 alert_rules；已有数据要按规则补齐，
    # 否则老触发记录会全部落到 0，切到隔离模式后看不见自己历史的预警。
    if _has_column("alert_triggers", "owner_id"):
        op.execute(
            "UPDATE alert_triggers SET owner_id = ("
            "SELECT owner_id FROM alert_rules WHERE alert_rules.id = alert_triggers.rule_id"
            ") WHERE owner_id = 0 AND rule_id IN (SELECT id FROM alert_rules WHERE owner_id != 0)"
        )


def downgrade() -> None:
    """退回共享语义：先恢复旧唯一约束，再删列（顺序反了会重建出带 owner 的表）。"""
    existing = _unique_constraint("watchlist_items", WATCHLIST_UQ)
    if existing is not None and existing != ["list_name", "code"]:
        with op.batch_alter_table("watchlist_items", schema=None) as batch:
            batch.drop_constraint(WATCHLIST_UQ, type_="unique")
            batch.create_unique_constraint(WATCHLIST_UQ, ["list_name", "code"])

    for table in reversed(OWNED_TABLES):
        if _has_column(table, "owner_id"):
            with op.batch_alter_table(table, schema=None) as batch:
                batch.drop_column("owner_id")
