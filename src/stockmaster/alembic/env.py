"""Alembic 运行环境.

两个刻意的开关：

* ``render_as_batch=True`` —— SQLite 不支持 ``ALTER COLUMN`` / ``DROP CONSTRAINT``，
  所有改列、改唯一约束的迁移都必须走 ``batch_alter_table``（建新表→搬数据→换名）。
  没有它，用户隔离要做的 ``watchlist_items`` 唯一键改造会直接报语法错误。
* ``compare_type=True`` —— 价格与金额列的类型收窄不会被 autogenerate 静默忽略。

URL 来源优先级：程序化传入的 ``sqlalchemy.url``（把旧备份恢复/迁移到另一路径时用）
→ ``settings.database_url``。生产镜像里 ``REPO_ROOT`` 是 ``/app``，与 ini 的
``%(here)s`` 一致。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 必须显式导入：autogenerate 只认识已注册到 Base.metadata 的模型。
import stockmaster.db.models  # noqa: F401
from stockmaster.config import settings
from stockmaster.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url", ""):
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（``alembic upgrade --sql``）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接真实数据库逐条应用迁移。"""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            # 每个 migration 一个事务：中途失败时已完成的版本保持有效，
            # 重跑只补未应用的那几个，而不是整链回滚。
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
