"""Alembic 迁移链测试：新库打标、存量库认领、幂等、漂移报告。

不测「某个迁移的 SQL 写对了」——那属于迁移自身的验收；这里测的是桥接层的行为，
因为存量库认领（stamp）与升级（upgrade）走错分支时的故障很隐蔽：不报错，但 schema
停在旧版本，之后某个查询才炸 ``no such column``。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"mig-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import session as session_mod

    session_mod.reset_engine()
    yield tmp_path
    session_mod.reset_engine()
    get_settings.cache_clear()


def _version_rows() -> int:
    from app.config import settings

    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def test_fresh_database_is_stamped_to_head(db_env):
    from app.db import init_db, migrations

    init_db()

    assert migrations.current_revision() == migrations.head_revision()
    assert _version_rows() == 1


def test_legacy_database_without_version_table_is_adopted(db_env):
    """create_all 时代的库没有 alembic_version：认领而不是重放 baseline。"""
    import app.db.models  # noqa: F401 - 注册元数据
    from app.db import session as session_mod
    from app.db.base import Base
    from app.db.session import get_engine

    Base.metadata.create_all(get_engine())

    from app.db import migrations

    # 有业务表但没有版本表：正是存量库的判据
    assert migrations.current_revision() is None

    session_mod.reset_engine()
    session_mod.init_db()

    assert migrations.current_revision() == migrations.head_revision()
    assert _version_rows() == 1


def test_ensure_migrated_is_idempotent(db_env):
    from app.db import init_db, migrations

    init_db()
    first = migrations.current_revision()

    init_db()
    init_db()

    assert migrations.current_revision() == first
    assert _version_rows() == 1


def test_history_marks_every_applied_revision(db_env):
    """已应用 = 当前版本及其全部祖先，不是只有最后一条。"""
    from app.db import init_db, migrations

    init_db()
    rows = migrations.history()

    revisions = [row["revision"] for row in rows]
    assert revisions[0] == "0001_baseline"
    assert revisions[-1] == migrations.head_revision()
    assert all(row["applied"] for row in rows), "升级到 head 后整条链都算已应用"


def test_schema_drift_reports_missing_column(db_env):
    """迁移链漏写时，自检要能指出来（而不是等业务查询报错）。"""
    from app.db import init_db, migrations

    init_db()
    assert migrations.schema_drift() == []

    from app.config import settings

    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("ALTER TABLE users DROP COLUMN nickname")
        conn.commit()
    except sqlite3.OperationalError:  # pragma: no cover - 老版本 SQLite 不支持 DROP COLUMN
        pytest.skip("SQLite 版本不支持 DROP COLUMN")
    finally:
        conn.close()

    drift = migrations.schema_drift()
    assert any("users.nickname" in item for item in drift)


def test_auto_migrate_can_be_disabled(tmp_path, monkeypatch):
    """关闭自动迁移时仍要建表，但不能偷偷改版本。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"mig-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_DB_AUTO_MIGRATE", "false")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    from app.db import migrations

    # 表建了，但版本记录没有 → 状态仍是「未纳管」，由运维显式 db upgrade
    assert migrations.current_revision() is None

    session_mod.reset_engine()
    get_settings.cache_clear()
