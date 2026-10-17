"""FastAPI dependencies for authentication and authorisation.

The gate is intentionally *opt-in*: with ``SM_AUTH_ENABLED=false`` the API stays
open (single-machine mode) yet a valid token is still honoured, so the UI can be
logged in and accounts provisioned before the switch is flipped.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth import tokens
from app.config import settings
from app.core.logging import get_logger
from app.db.models import User
from app.db.scoped import GUEST_OWNER_ID
from app.db.session import get_db

log = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access token>")


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #


def client_ip(request: Request) -> str:
    """Best-effort client address, honouring a single proxy hop."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()[:64]
    return (request.client.host if request.client else "")[:64]


def client_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:256]


def base_url(request: Request) -> str:
    """Public origin, honouring reverse-proxy headers.

    Uvicorn only trusts ``X-Forwarded-*`` from localhost by default, and the app
    sits behind the cndb nginx container, so the headers are read explicitly —
    otherwise the WeChat callback URL would be built with the wrong scheme and the
    platform would reject the redirect.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    scheme = proto or request.url.scheme
    if host:
        return f"{scheme}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def unauthorized(detail: str, code: str = "unauthorized") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer", "X-Auth-Error": code},
    )


# --------------------------------------------------------------------------- #
# Token -> user
# --------------------------------------------------------------------------- #


def resolve_token_user(session: Session, token: str) -> User:
    """Validate a bearer token and return the matching active account."""
    try:
        payload = tokens.decode(token)
    except tokens.TokenError as exc:
        detail = "登录已过期，请重新登录" if exc.reason == "expired" else "登录凭证无效"
        raise unauthorized(detail, exc.reason) from exc

    user = session.get(User, payload.user_id)
    if user is None:
        raise unauthorized("账号不存在", "user_missing")
    if payload.token_version != user.token_version:
        # Password change / forced logout bumps this counter.
        raise unauthorized("登录状态已失效，请重新登录", "token_revoked")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用，请联系管理员",
            headers={"X-Auth-Error": "user_disabled"},
        )
    return user


def current_user(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User | None:
    """The caller's account, or ``None`` when anonymous.

    A token supplied while the gate is off is still resolved, so the UI behaves
    the same in both modes. An *invalid* token is an error rather than a silent
    downgrade — otherwise an expired session would look like "lost permissions".
    """
    # Set by AuthGateMiddleware when the gate is on; avoids re-verifying.
    gated_user_id = getattr(request.state, "auth_user_id", None)
    if gated_user_id is not None:
        user = session.get(User, gated_user_id)
        if user is None:
            raise unauthorized("账号不存在", "user_missing")
        return user

    token = credentials.credentials if credentials else None
    if not token:
        return None
    return resolve_token_user(session, token)


def require_user(user: Annotated[User | None, Depends(current_user)]) -> User:
    """Guest sessions are rejected; every route behind this needs a real account."""
    if user is None:
        raise unauthorized("请先登录")
    return user


def current_owner(user: Annotated[User | None, Depends(current_user)]) -> int:
    """The ``owner_id`` the caller reads and writes user data under.

    Three cases, deliberately collapsed into one integer:

    * isolation off (default) → always ``0``, i.e. the historical shared bucket.
      Every pre-isolation row is already ``0``, so the API behaves exactly as it
      did before the feature landed;
    * isolation on + logged in → the account id;
    * isolation on + anonymous → ``0`` as well. The gate normally prevents this,
      but with ``SM_AUTH_ENABLED=false`` an anonymous caller must still see the
      legacy data instead of an empty app.
    """
    if not settings.data_isolation:
        return GUEST_OWNER_ID
    return user.id if user is not None else GUEST_OWNER_ID


def local_guest_admin() -> User:
    """Synthetic admin used when authentication is switched off.

    The account pages must remain usable in single-machine mode, otherwise there
    is no way to create the first user before enabling the gate.
    """
    return User(id=0, username="local", nickname="本机管理员", role="admin", status="active")


def require_admin(user: Annotated[User | None, Depends(current_user)]) -> User:
    """Administrator-only routes."""
    if user is None:
        if settings.auth_enabled:
            raise unauthorized("请先登录")
        return local_guest_admin()
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
            headers={"X-Auth-Error": "forbidden"},
        )
    return user


__all__ = [
    "base_url",
    "bearer_scheme",
    "client_agent",
    "client_ip",
    "current_owner",
    "current_user",
    "local_guest_admin",
    "require_admin",
    "require_user",
    "resolve_token_user",
    "unauthorized",
]
