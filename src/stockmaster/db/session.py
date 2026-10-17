"""Engine/session management with SQLite tuning suitable for analytics workloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from stockmaster.config import settings
from stockmaster.core.logging import get_logger
from stockmaster.db.base import Base

log = get_logger(__name__)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    """Apply pragmas per connection.

    * ``WAL`` lets readers proceed while a long sync writes.
    * ``synchronous=NORMAL`` is the standard durability/perf trade-off under WAL.
    * ``foreign_keys=ON`` is required for the ON DELETE CASCADE constraints.
    * ``busy_timeout`` avoids spurious "database is locked" during concurrent jobs.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-64000")  # ~64 MB page cache
        cur.execute("PRAGMA mmap_size=268435456")  # 256 MB
    finally:
        cur.close()


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine  # noqa: PLW0603
    if _engine is None:
        settings.ensure_dirs()
        _engine = create_engine(
            settings.database_url,
            echo=False,
            future=True,
            # SQLite + threads: sessions are used from sync job threads.
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )
        event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal  # noqa: PLW0603
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session (no implicit commit)."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Bring the schema to the current version. Idempotent.

    Order matters: ``create_all`` first (a brand-new database gets every table in
    one shot), then the Alembic bridge, which either stamps an existing
    un-versioned database or applies pending deltas. Doing it the other way round
    would make a fresh install replay migrations against an empty database.

    The model module is imported explicitly so ``Base.metadata`` is fully
    populated even when a caller reaches ``init_db`` without importing ``app.db``
    first. Without that, ``create_all`` would silently build a partial schema.
    """
    settings.ensure_dirs()
    import stockmaster.db.models  # noqa: F401 - registers ORM mappers
    from stockmaster.db.migrations import current_revision, ensure_migrated, head_revision

    engine = get_engine()
    if settings.db_auto_migrate:
        ensure_migrated(engine)
    else:
        # 运维要求「先显式迁移再启动」时：只建缺失的表，不改版本、不跑增量。
        # 版本落后要吭声，否则会以「少一列」的形式在业务查询里才爆出来。
        Base.metadata.create_all(engine)
        current, head = current_revision(engine=engine), head_revision()
        if current != head:
            log.warning(
                "SM_DB_AUTO_MIGRATE=false 且数据库版本 %s ≠ 最新 %s；请执行 `stockmaster db upgrade`",
                current or "未纳管",
                head,
            )

    # Verify the schema is complete rather than trusting create_all.
    from sqlalchemy import inspect

    present = set(inspect(engine).get_table_names())
    missing = set(Base.metadata.tables) - present
    if missing:
        raise RuntimeError(f"database schema incomplete; missing tables: {sorted(missing)}")
    log.info("database ready at %s (%d tables)", settings.db_path, len(present))


def vacuum() -> None:
    """Compact the database (call after large backfills/deletes)."""
    with get_engine().connect() as conn:
        conn.execute(text("VACUUM"))
    log.info("vacuum complete")


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests)."""
    global _engine, _SessionLocal  # noqa: PLW0603
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
