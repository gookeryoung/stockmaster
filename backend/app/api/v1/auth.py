"""Login, registration, session and WeChat OAuth endpoints."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import db_session
from app.auth import passwords, service
from app.auth.deps import base_url, client_agent, client_ip, current_user, require_user
from app.auth.wechat import WechatError, build_client
from app.config import settings
from app.core.logging import get_logger
from app.db.models import User
from app.schemas.auth import (
    AuthConfigOut,
    IdentityOut,
    LoginIn,
    PasswordChangeIn,
    ProfileUpdateIn,
    RegisterIn,
    TicketExchangeIn,
    TokenOut,
    UserOut,
    WechatUrlOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[Session, Depends(db_session)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to the naive datetimes SQLite hands back."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def user_out(session: Session, user: User, *, with_identities: bool = False) -> UserOut:
    identities = (
        [IdentityOut.model_validate(item) for item in service.list_identities(session, user.id)]
        if with_identities and user.id
        else []
    )
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        nickname=user.nickname,
        avatar_url=user.avatar_url,
        role=user.role,  # type: ignore[arg-type]
        status=user.status,  # type: ignore[arg-type]
        last_login_at=as_utc(user.last_login_at),
        created_at=as_utc(user.created_at),
        has_password=bool(user.password_hash),
        identities=identities,
    )


def _token_out(session: Session, user: User) -> TokenOut:
    result = service.issue_session(user)
    return TokenOut(
        token=result.token,
        expires_at=result.expires_at.replace(tzinfo=UTC),
        user=user_out(session, user, with_identities=True),
    )


def _fail(exc: service.AuthError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.message,
        headers={"X-Auth-Error": exc.code},
    )


def _callback_url(request: Request) -> str:
    """Exact redirect URI registered on the WeChat platform."""
    configured = settings.wechat_redirect_uri.strip()
    if configured:
        return configured
    return f"{base_url(request)}/api/v1/auth/wechat/callback"


def _frontend_redirect(request: Request, path: str, params: dict[str, str]) -> RedirectResponse:
    """Bounce back into the SPA, honouring SM_AUTH_FRONTEND_BASE when set.

    The dev server runs on a different port than the API, so the origin cannot
    always be inferred from the incoming request.
    """
    origin = settings.auth_frontend_base.strip().rstrip("/") or base_url(request)
    query = urlencode(params)
    return RedirectResponse(f"{origin}{path}?{query}", status_code=status.HTTP_302_FOUND)


# --------------------------------------------------------------------------- #
# Configuration & credentials
# --------------------------------------------------------------------------- #


@router.get("/config", response_model=AuthConfigOut)
def auth_config(session: SessionDep):
    """Public bootstrap info for the login page."""
    return AuthConfigOut(
        auth_enabled=settings.auth_enabled,
        allow_register=settings.auth_allow_register,
        wechat_enabled=settings.wechat_enabled,
        wechat_mode=settings.wechat_mode,
        has_users=service.count_users(session) > 0,
        version=__version__,
    )


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, request: Request, session: SessionDep):
    """Self-registration with username + password."""
    if not settings.auth_allow_register:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前已关闭自助注册，请联系管理员开通账号",
        )
    try:
        user = service.create_user(
            session,
            username=payload.username,
            password=payload.password,
            email=payload.email,
            nickname=payload.nickname,
        )
    except service.AuthError as exc:
        raise _fail(exc) from exc

    service.audit(
        session,
        "user.register",
        user=user,
        ip=client_ip(request),
        user_agent=client_agent(request),
    )
    service.touch_login(session, user, client_ip(request))
    return _token_out(session, user)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, session: SessionDep):
    """Password login by username or email."""
    try:
        user = service.authenticate(session, payload.username, payload.password, ip=client_ip(request))
    except service.AuthError as exc:
        raise _fail(exc) from exc
    return _token_out(session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user)],
):
    """Invalidate every token of the caller (all devices)."""
    if user is None:
        return
    service.revoke_tokens(session, user)
    service.audit(session, "logout", user=user, ip=client_ip(request))


# --------------------------------------------------------------------------- #
# Self-service
# --------------------------------------------------------------------------- #


@router.get("/me", response_model=UserOut)
def me(session: SessionDep, user: Annotated[User, Depends(require_user)]):
    return user_out(session, user, with_identities=True)


@router.patch("/me", response_model=UserOut)
def update_me(payload: ProfileUpdateIn, session: SessionDep, user: Annotated[User, Depends(require_user)]):
    """Update the caller's own profile fields."""
    if payload.nickname is not None:
        user.nickname = payload.nickname.strip()[:64]
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url.strip()[:512]
    if payload.email is not None:
        email = service.normalize_email(payload.email)
        if email and email != (user.email or ""):
            other = service.get_user_by_email(session, email)
            if other is not None and other.id != user.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已被使用")
        user.email = email
    session.commit()
    session.refresh(user)
    return user_out(session, user, with_identities=True)


@router.post("/me/password", response_model=TokenOut)
def change_password(
    payload: PasswordChangeIn,
    request: Request,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
):
    """Change the password.

    A fresh token is returned because changing the password bumps
    ``token_version`` and therefore invalidates the caller's current one.
    """
    if user.password_hash and not passwords.verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确")
    try:
        passwords.validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    service.set_password(session, user, payload.new_password)
    service.audit(session, "password.change", user=user, ip=client_ip(request))
    return _token_out(session, user)


@router.get("/me/identities", response_model=list[IdentityOut])
def my_identities(session: SessionDep, user: Annotated[User, Depends(require_user)]):
    return [IdentityOut.model_validate(item) for item in service.list_identities(session, user.id)]


@router.delete("/me/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
def unbind(
    identity_id: int,
    request: Request,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
):
    try:
        service.unbind_identity(session, user, identity_id)
    except service.AuthError as exc:
        raise _fail(exc) from exc
    service.audit(session, "wechat.unbind", user=user, ip=client_ip(request))


# --------------------------------------------------------------------------- #
# WeChat OAuth
# --------------------------------------------------------------------------- #


@router.get("/wechat/url", response_model=WechatUrlOut)
def wechat_authorize_url(
    request: Request,
    session: SessionDep,
    redirect: str = Query(default="/", max_length=512),
    bind: bool = Query(default=False, description="绑定到当前登录账号，而不是登录"),
):
    """Issue a state and return the WeChat authorisation URL for the browser."""
    mode = settings.wechat_mode
    if mode == "off":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="微信登录未配置：请设置 SM_WECHAT_APP_ID / SM_WECHAT_APP_SECRET，"
            "或在本地用 SM_WECHAT_LOGIN_MODE=mock 演练流程",
        )

    bind_user: User | None = None
    if bind:
        token = request.headers.get("authorization", "")[7:].strip()
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="绑定微信前请先登录")
        from app.auth.deps import resolve_token_user

        bind_user = resolve_token_user(session, token)

    state = service.create_flow_state(
        session,
        kind="oauth",
        provider="wechat",
        redirect_to=service.safe_redirect(redirect),
        payload={"bind_user_id": bind_user.id} if bind_user else {},
    )
    client = build_client(_callback_url(request))
    return WechatUrlOut(url=client.authorize_url(state), state=state, mode=mode)


@router.get("/wechat/mock/authorize", response_class=HTMLResponse, include_in_schema=False)
def wechat_mock_authorize(
    request: Request,
    state: str = Query(default=""),
    name: str = Query(default="demo"),
):
    """Stand-in for the WeChat QR page, for local development only.

    Enabled exclusively by ``SM_WECHAT_LOGIN_MODE=mock``: no credentials exist to
    test against, and the rest of the callback path (state, ticket, account
    creation) is identical to production, so this exercises real code.
    """
    if settings.wechat_mode != "mock":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mock 授权未启用")

    callback = _callback_url(request)
    safe_state = html.escape(state, quote=True)
    safe_name = html.escape(name, quote=True)
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟微信授权</title>
<style>
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   background:#0f172a;color:#e2e8f0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
 .card{{width:min(92vw,380px);background:#1e293b;border:1px solid #334155;border-radius:14px;padding:22px}}
 h1{{font-size:16px;margin:0 0 6px}} p{{font-size:12px;color:#94a3b8;margin:0 0 16px;line-height:1.6}}
 input{{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #475569;
   background:#0f172a;color:#e2e8f0;font-size:13px;margin-bottom:12px}}
 button{{width:100%;padding:10px;border:0;border-radius:8px;background:#07c160;color:#fff;
   font-size:14px;font-weight:600;cursor:pointer}}
 a{{display:block;text-align:center;margin-top:12px;font-size:12px;color:#64748b}}
</style></head><body><div class="card">
<h1>微信授权（模拟）</h1>
<p>当前为 SM_WECHAT_LOGIN_MODE=mock，未接入真实微信。<br>
不同昵称对应不同模拟 openid，可用于验证账号创建与绑定。</p>
<form method="get" action="{html.escape(callback, quote=True)}">
  <input type="hidden" name="state" value="{safe_state}">
  <input name="code" value="mock:{safe_name}" aria-label="模拟 openid">
  <button type="submit">确认授权</button>
</form>
<a href="{html.escape(callback, quote=True)}?state={safe_state}&amp;error=access_denied">取消</a>
</div></body></html>"""
    return HTMLResponse(body)


@router.get("/wechat/callback", include_in_schema=False)
def wechat_callback(
    request: Request,
    session: SessionDep,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
):
    """WeChat redirects the browser here; finish the handshake and bounce back.

    Failures are redirected to the login page with a readable message rather than
    shown as JSON, because the user is in a browser navigation, not an XHR.
    """
    try:
        flow = service.take_flow_state(session, state, kind="oauth")
    except service.FlowStateError as exc:
        return _frontend_redirect(request, "/login", {"error": exc.message})

    redirect_to = flow.redirect_to or "/"
    if error or not code:
        return _frontend_redirect(request, "/login", {"error": "已取消微信授权"})

    client = build_client(_callback_url(request))
    try:
        profile = client.exchange_code(code)
    except WechatError as exc:
        log.warning("wechat oauth failed: %s", exc)
        return _frontend_redirect(request, "/login", {"error": str(exc)})

    bind_user_id = 0
    try:
        flow_payload = json.loads(flow.payload_json or "{}")
        bind_user_id = int(flow_payload.get("bind_user_id") or 0)
    except (ValueError, TypeError):
        bind_user_id = 0

    try:
        if bind_user_id:
            target = service.get_user(session, bind_user_id)
            if target is None:
                return _frontend_redirect(request, "/login", {"error": "绑定目标账号不存在"})
            service.bind_wechat(session, target, profile)
            user = target
        else:
            user, _created, _identity = service.upsert_wechat_user(session, profile)
            service.touch_login(session, user, client_ip(request))
            service.audit(session, "login.success", user=user, detail="wechat", ip=client_ip(request))
    except service.AuthError as exc:
        return _frontend_redirect(request, "/login", {"error": exc.message})

    ticket = service.create_flow_state(
        session,
        kind="ticket",
        provider="wechat",
        redirect_to=redirect_to,
        user_id=user.id,
        ttl_seconds=settings.auth_ticket_ttl_seconds,
    )
    params = {"ticket": ticket, "next": redirect_to}
    if bind_user_id:
        params["bound"] = "1"
    return _frontend_redirect(request, "/auth/callback", params)


@router.post("/ticket", response_model=TokenOut)
def exchange_ticket(payload: TicketExchangeIn, request: Request, session: SessionDep):
    """Trade the one-time ticket from the OAuth redirect for a real token."""
    try:
        flow = service.take_flow_state(session, payload.ticket, kind="ticket")
    except service.FlowStateError as exc:
        raise _fail(exc) from exc

    user = service.get_user(session, flow.user_id or 0)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在")
    try:
        token_out = _token_out(session, user)
    except service.AuthError as exc:
        raise _fail(exc) from exc
    service.audit(session, "login.ticket", user=user, ip=client_ip(request))
    return token_out
