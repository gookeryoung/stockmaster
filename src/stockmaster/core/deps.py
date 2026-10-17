"""Shared FastAPI dependencies and helpers."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from stockmaster.core.errors import DataError, SymbolNotFound
from stockmaster.core.symbols import normalize
from stockmaster.db.session import get_db

# Aliased rather than wrapped: FastAPI caches a dependency per request *by
# callable*, so a wrapper would create a second session and any ORM instance
# loaded by one (e.g. the authenticated user) would be detached in the other.
db_session = get_db


def canonical_code(code: str) -> str:
    """Validate and normalise a symbol path parameter.

    Raises:
        HTTPException: 422 when the symbol cannot be normalised.
    """
    try:
        return normalize(code)
    except DataError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid symbol: {exc}",
        ) from exc


def require_instrument(session: Session, code: str):
    """Fetch an instrument or raise 404."""
    from stockmaster.sync import repository as repo

    instrument = repo.get_instrument(session, code)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"instrument {code} not found; sync the security master first",
        )
    return instrument


def resolve_universe(limit: int = Query(default=300, ge=1, le=5000)) -> int:
    """Query-parameter signature for endpoints taking a universe size."""
    return limit


__all__ = [
    "Depends",
    "HTTPException",
    "Query",
    "Session",
    "SymbolNotFound",
    "canonical_code",
    "db_session",
    "require_instrument",
    "resolve_universe",
    "status",
]
