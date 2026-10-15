"""Engine/session management with SQLite tuning suitable for analytics workloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.core.logging import get_logger
from app.db.base import Base

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
    global _engine
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
    global _SessionLocal
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
    """Create all tables. Idempotent; Alembic is used for real migrations.

    Imports the model module explicitly so ``Base.metadata`` is fully populated
    even when a caller reaches ``init_db`` without importing ``app.db`` first.
    Without that, ``create_all`` would silently build a partial schema.
    """
    settings.ensure_dirs()
    import app.db.models  # noqa: F401,PLC0415 - registers mappers as a side effect

    engine = get_engine()
    Base.metadata.create_all(engine)

    # Verify the schema is complete rather than trusting create_all.
    from sqlalchemy import inspect  # noqa: PLC0415

    present = set(inspect(engine).get_table_names())
    missing = set(Base.metadata.tables) - present
    if missing:
        raise RuntimeError(
            f"database schema incomplete; missing tables: {sorted(missing)}"
        )
    log.info("database ready at %s (%d tables)", settings.db_path, len(present))


def vacuum() -> None:
    """Compact the database (call after large backfills/deletes)."""
    with get_engine().connect() as conn:
        conn.execute(text("VACUUM"))
    log.info("vacuum complete")


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
