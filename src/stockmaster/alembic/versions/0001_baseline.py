"""Baseline —— 迁移链的起点，不含任何 DDL.

为什么是空的
------------
StockMaster 的 schema 唯一真源是 ORM 模型（``Base.metadata``）：``init_db()`` 用
``create_all`` 建表，线上每个库都是这么来的。如果 baseline 里再抄一份 20 张表的
``op.create_table``，就会立刻出现两份互相漂移的定义 —— 加了列忘了改迁移，或者改了
迁移忘了改模型，两边都能跑，直到某次恢复才炸。

因此本仓库的约定是：

* baseline 只负责给「存量库」一个可以 ``stamp`` 的锚点；
* 迁移链只记录**增量**（加列、改约束、数据回填）；
* 建库顺序永远是 ``create_all`` → ``ensure_migrated``（见 ``app/db/migrations.py``），
  而不是「只靠 upgrade head 建库」。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """无操作：baseline 仅标记「此库已纳入迁移管理」。"""


def downgrade() -> None:
    """无操作：downgrade 到 baseline 之下意味着放弃整条迁移链。"""
