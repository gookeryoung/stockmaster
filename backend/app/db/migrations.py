"""Alembic 桥接层：把迁移链接到应用生命周期上。

设计取舍
--------
schema 的真源仍是 ORM 模型，迁移链只承载增量。于是建库流程是::

    has_application_schema()   # 建表之前判空库，之后判不出来
    create_all()               # 新库建齐；存量库只补缺失的表
    ensure_migrated()          # 见下表
    create_all()               # 补建迁移链从未覆盖过的新模型表

三种情形都能落到同一个状态，且**任何一步都能重复执行**：

===============  ====================================  ==========================
库的状态          ensure_migrated 的行为                  结果
===============  ====================================  ==========================
全新空库          ``create_all`` 已是 head → stamp head    与模型一致
存量库（旧版）     stamp 到 baseline，再 ``upgrade head``   增量迁移真正执行
已纳管库          按 current → head 逐步 upgrade            应用增量
===============  ====================================  ==========================

存量库不能只 stamp 到 head：那样 ``0002`` 的加列会被整条跳过，库自称「最新」却缺列，
错误会跑到业务查询里才出现（``no such column``）。相应地，迁移脚本必须写成幂等的，
因为它既可能在「列已存在」的全新库上跑，也可能在「列不存在」的存量库上跑。

调用 ``command.*`` 时自建 ``Config``（不读 alembic.ini）：容器与测试环境里 ini 未必
在工作目录，而且我们不希望 alembic 的 logging 配置覆盖应用的日志设置。alembic.ini
仍然保留，供开发者直接使用 ``alembic revision --autogenerate``。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect

from app.config import REPO_ROOT, settings
from app.core.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = REPO_ROOT / "backend" / "migrations"

# Alembic 的 runtime 日志继承 root logger：不压到 WARNING 的话，每次迁移都会把
# 七个 autogenerate 插件的装配过程打到控制台。迁移结果由下面自己的 log 输出。
logging.getLogger("alembic").setLevel(logging.WARNING)


class MigrationError(RuntimeError):
    """迁移无法继续（版本链断裂、库不可写等）。"""


def _config(url: str | None = None):
    """构造 Alembic 配置；``url`` 缺省用当前 settings。"""
    from alembic.config import Config

    if not (MIGRATIONS_DIR / "env.py").is_file():  # pragma: no cover - 打包异常
        raise MigrationError(f"迁移目录缺失: {MIGRATIONS_DIR}")

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # 路径里的 % 会被 configparser 当插值符号，SQLite 路径不会出现，但仍做一次保护
    cfg.set_main_option("sqlalchemy.url", (url or settings.database_url).replace("%", "%%"))
    return cfg


def head_revision(url: str | None = None) -> str | None:
    """迁移链的最新版本号。"""
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(_config(url))
    except Exception as exc:  # pragma: no cover - 目录损坏
        raise MigrationError(f"无法读取迁移链: {exc}") from exc
    return script.get_current_head()


def current_revision(url: str | None = None, engine: Engine | None = None) -> str | None:
    """数据库当前版本号；``alembic_version`` 表不存在时返回 ``None``。

    这就是「这个库有没有被纳入迁移管理」的判据 —— 与表是否存在无关，因为存量库
    有业务表但没有版本表。
    """
    from alembic.runtime.migration import MigrationContext

    own_engine = None
    target = engine
    if target is None:
        own_engine = create_engine(url or settings.database_url, future=True)
        target = own_engine
    try:
        with target.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:
        raise MigrationError(f"读取迁移版本失败: {exc}") from exc
    finally:
        if own_engine is not None:
            own_engine.dispose()


def stamp_head(url: str | None = None) -> str | None:
    """把版本表直接标到 head，不执行任何迁移（认领存量库用）。"""
    from alembic import command

    head = head_revision(url)
    command.stamp(_config(url), "head")
    log.info("迁移版本已标记为 head: %s", head)
    return head


def base_revision(url: str | None = None) -> str | None:
    """迁移链的起点版本号（第一个迁移）。"""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(url))
    bases = script.get_bases()
    return bases[0] if bases else None


def stamp_revision(revision: str, url: str | None = None) -> None:
    """把版本标记到指定 revision，不执行迁移。"""
    from alembic import command

    command.stamp(_config(url), revision)


def upgrade_to_head(url: str | None = None) -> str | None:
    """把库升级到 head。"""
    from alembic import command

    head = head_revision(url)
    command.upgrade(_config(url), "head")
    log.info("数据库已升级到 %s", head)
    return head


def history(url: str | None = None) -> list[dict[str, str | bool]]:
    """按时间顺序列出迁移链，标注是否已应用。

    已应用的是「当前版本及其全部祖先」，不是只有当前那一条 —— 否则链一长，
    ``history`` 会显示成「只跑过最后一步」，看着像中间步骤全丢了。
    """
    from alembic.script import ScriptDirectory

    cfg = _config(url)
    applied = current_revision(url)
    script = ScriptDirectory.from_config(cfg)
    done: set[str] = set()
    if applied:
        done = {rev.revision for rev in script.iterate_revisions(applied, "base")}
    rows: list[dict[str, str | bool]] = []
    for rev in script.walk_revisions():  # head → base
        rows.append(
            {
                "revision": rev.revision,
                "down_revision": rev.down_revision or "",
                "description": (rev.doc or "").strip().splitlines()[0] if rev.doc else "",
                "applied": rev.revision in done,
            }
        )
    rows.reverse()
    return rows


def schema_drift(engine: Engine | None = None) -> list[str]:
    """比对 ORM 元数据与实际库，返回缺失的表/列描述。

    只**报告**不修复：自动 ADD COLUMN 无法判断默认值与非空约束，静默改 schema 比
    报错更危险。恢复旧备份后、或 ``stamp`` 认领了不一致的库时，用这个自检。
    """
    from app.db.base import Base

    own_engine = None
    target = engine
    if target is None:
        own_engine = create_engine(settings.database_url, future=True)
        target = own_engine
    problems: list[str] = []
    try:
        inspector = inspect(target)
        present = set(inspector.get_table_names())
        for table_name, table in Base.metadata.tables.items():
            if table_name not in present:
                problems.append(f"缺表 {table_name}")
                continue
            actual = {col["name"] for col in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name not in actual:
                    problems.append(f"缺列 {table_name}.{column.name}")
    finally:
        if own_engine is not None:
            own_engine.dispose()
    return problems


def has_application_schema(engine: Engine) -> bool:
    """库里是否已经存在本项目的业务表（用于区分「空库」与「存量库」）。

    必须在 ``create_all`` **之前** 调用：建表之后两者无法区分，而区分错了会走错
    分支 —— 空库被当成存量库会去重放增量（撞 duplicate column），存量库被当成空库
    会直接打标到 head（缺列留到业务查询才炸）。
    """
    from app.db.base import Base

    present = set(inspect(engine).get_table_names())
    return bool(present & set(Base.metadata.tables))


def ensure_migrated(engine: Engine | None = None) -> str | None:
    """启动期入口：把库带到「已纳管且等于 head」的状态。

    幂等。三种情形：

    * **空库**：``create_all`` 已按当前模型建齐，直接 stamp 到 head（迁移链不需要
      再跑一遍，跑了反而会撞 duplicate column）。
    * **存量库（有表无版本表）**：先 stamp 到 **baseline**，再 ``upgrade head``。
      只 stamp head 是不行的 —— 那样所有增量迁移（比如 ``0002`` 的加列）都会被
      跳过，库看起来「已是最新」，实际缺列。
    * **已纳管库**：正常 ``upgrade head``。
    """
    from app.db.base import Base

    if engine is None:
        from app.db.session import get_engine

        engine = get_engine()  # 复用应用连接池，不额外开引擎
    # 迁移动作用的是独立引擎，必须指向同一个库，否则会 upgrade 到 settings 的默认路径。
    target_url = str(engine.url)

    # 先判空库，再建表 —— 顺序反了就只剩「有表」这一个错误结论。
    fresh = not has_application_schema(engine)
    Base.metadata.create_all(engine)

    head = head_revision(target_url)
    current = current_revision(engine=engine)
    if current == head:
        return current

    if current is None:
        base = base_revision(target_url)
        if fresh or base is None or base == head:
            log.info("新库或单步迁移链，直接标记为 %s", head)
            stamp_head(target_url)
        else:
            log.info("存量库认领：先标记 %s，再应用增量到 %s", base, head)
            stamp_revision(base, target_url)
            upgrade_to_head(target_url)
    else:
        log.info("数据库 %s → %s，开始应用迁移", current, head)
        upgrade_to_head(target_url)

    # 迁移链可能有缺口（历史上只改模型没写迁移），补建缺失的表；已存在的表不受影响。
    Base.metadata.create_all(engine)

    drift = schema_drift(engine)
    if drift:
        log.warning(
            "schema 与模型存在 %d 处差异（迁移链可能不完整）：%s%s",
            len(drift),
            ", ".join(drift[:5]),
            " ..." if len(drift) > 5 else "",
        )
    return current_revision(engine=engine)


def alembic_ini_path() -> Path:
    """给 CLI/文档展示用。"""
    return REPO_ROOT / "alembic.ini"


__all__ = [
    "MIGRATIONS_DIR",
    "MigrationError",
    "alembic_ini_path",
    "base_revision",
    "current_revision",
    "ensure_migrated",
    "has_application_schema",
    "head_revision",
    "history",
    "schema_drift",
    "stamp_head",
    "stamp_revision",
    "upgrade_to_head",
]
