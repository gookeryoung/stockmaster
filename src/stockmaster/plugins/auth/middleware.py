"""HTTP gate that enforces login when ``SM_AUTH_ENABLED=true``.

Implemented as middleware rather than a per-router dependency because every
``/api/v1`` router must be covered — including ones added later. The gate only
*authenticates*: anything that needs the account object itself uses the
``current_user`` dependency, which reuses the id resolved here.
"""

from __future__ import annotations

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_401_UNAUTHORIZED

from stockmaster.config import settings
from stockmaster.core.auth_deps import resolve_token_user
from stockmaster.core.logging import get_logger
from stockmaster.db.session import get_session_factory

log = get_logger(__name__)

API_PREFIX = "/api/v1"

# Endpoints that must work before a session exists: login itself, the OAuth
# handshake, the one-time ticket exchange, and the liveness probe used by deploy
# scripts and container health checks.
PUBLIC_PATHS = frozenset(
    {
        f"{API_PREFIX}/health",
        f"{API_PREFIX}/auth/config",
        f"{API_PREFIX}/auth/login",
        f"{API_PREFIX}/auth/register",
        f"{API_PREFIX}/auth/ticket",
        f"{API_PREFIX}/auth/wechat/url",
        f"{API_PREFIX}/auth/wechat/callback",
        f"{API_PREFIX}/auth/wechat/mock/authorize",
    }
)


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated ``/api/v1`` calls with a JSON 401."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.auth_enabled or not self._needs_auth(request):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return self._reject("请先登录")

        token = header[7:].strip()
        session = get_session_factory()()
        try:
            user = resolve_token_user(session, token)
            request.state.auth_user_id = user.id
        except HTTPException as exc:
            headers = exc.headers or {}
            return self._reject(str(exc.detail), headers.get("X-Auth-Error", "unauthorized"))
        finally:
            session.close()

        return await call_next(request)

    @staticmethod
    def _needs_auth(request: Request) -> bool:
        if request.method == "OPTIONS":
            return False
        path = request.url.path
        if not path.startswith(API_PREFIX):
            return False
        return path.rstrip("/") not in PUBLIC_PATHS

    @staticmethod
    def _reject(detail: str, code: str = "unauthorized") -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={"detail": detail, "code": code},
            headers={"WWW-Authenticate": "Bearer"},
        )
