"""Database layer.

Importing this package registers **every** ORM model on ``Base.metadata``.

That side effect is load-bearing: ``init_db()`` calls ``Base.metadata.create_all()``,
which only creates tables whose model classes have actually been imported. If this
module did not import ``app.db.models``, a fresh database would be created with a
partial schema (only whatever happened to be imported by the caller) and fail
later with ``no such table`` errors.
"""

# Registration of all mapped classes — do not remove. See module docstring.
from app.db import models as models
from app.db.base import Base, utcnow
from app.db.session import (
    get_db,
    get_engine,
    get_session_factory,
    init_db,
    reset_engine,
    session_scope,
)

__all__ = [
    "Base",
    "get_db",
    "get_engine",
    "get_session_factory",
    "init_db",
    "models",
    "reset_engine",
    "session_scope",
    "utcnow",
]
