"""Account, session and OAuth state services.

Everything that touches the database lives here so the routers stay thin and the
same logic is reachable from the CLI (``stockmaster user ...``) and from tests.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stockmaster.config import settings
from stockmaster.core.logging import get_logger
from stockmaster.db.models import AuthAuditLog, AuthFlowState, User, UserIdentity
from stockmaster.plugins.auth.services import passwords, tokens
from stockmaster.plugins.auth.services.wechat import WechatProfile

log = get_logger(__name__)

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
RESERVED_USERNAMES = {"admin", "root", "system", "stockmaster"}
WECHAT_PROVIDER = "wechat"


class AuthError(Exception):
    """Authentication/authorisation failure carrying an HTTP-friendly message."""

    def __init__(self, message: str, *, code: str = "auth_error", status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class FlowStateError(AuthError):
    """OAuth ``state`` / login ticket missing, expired or already used."""

    def __init__(self, message: str = "登录会话已失效，请重新发起登录") -> None:
        super().__init__(message, code="flow_state_invalid", status_code=400)


@dataclass
class LoginResult:
    token: str
    expires_at: datetime
    user: User


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utcnow_naive() -> datetime:
    """UTC now as a naive datetime.

    SQLite columns here are timezone-naive (the dialect's DATETIME format drops
    the offset), so comparisons must be naive too or they silently never match.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def safe_redirect(target: str | None) -> str:
    """Reject absolute/protocol-relative URLs to keep the callback on-site."""
    candidate = (target or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return "/"
    return candidate[:512]


def normalize_username(raw: str) -> str:
    username = (raw or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise AuthError("用户名需 3-64 位，以字母或数字开头，仅含字母、数字、点、下划线和连字符")
    return username


def normalize_email(raw: str | None) -> str | None:
    email = (raw or "").strip()
    if not email:
        return None
    if "@" not in email or len(email) > 255 or email.startswith("@") or email.endswith("@"):
        raise AuthError("邮箱格式不正确")
    return email.lower()


# --------------------------------------------------------------------------- #
# Brute-force throttle (per process, sliding window)
# --------------------------------------------------------------------------- #

_failure_lock = threading.Lock()
_failure_log: dict[str, list[float]] = {}


def _throttle_key(username: str, ip: str) -> str:
    return f"{(username or '').lower()}|{ip or '-'}"


def check_throttle(username: str, ip: str) -> None:
    """Raise when too many failures happened inside the window."""
    window = settings.auth_failed_login_window
    limit = settings.auth_max_failed_logins
    if limit <= 0:
        return
    now = time.monotonic()
    with _failure_lock:
        stamps = [t for t in _failure_log.get(_throttle_key(username, ip), []) if now - t < window]
        _failure_log[_throttle_key(username, ip)] = stamps
    if len(stamps) >= limit:
        retry_after = int(window - (now - stamps[0])) + 1
        raise AuthError(
            f"登录失败次数过多，请 {max(retry_after, 1) // 60 + 1} 分钟后再试",
            code="too_many_attempts",
            status_code=429,
        )


def record_failure(username: str, ip: str) -> None:
    with _failure_lock:
        _failure_log.setdefault(_throttle_key(username, ip), []).append(time.monotonic())


def clear_failures(username: str, ip: str) -> None:
    with _failure_lock:
        _failure_log.pop(_throttle_key(username, ip), None)


def reset_throttle() -> None:
    """Drop all counters (tests)."""
    with _failure_lock:
        _failure_log.clear()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def get_user_by_username(session: Session, username: str) -> User | None:
    if not username:
        return None
    return session.execute(select(User).where(func.lower(User.username) == username.lower())).scalar_one_or_none()


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.execute(select(User).where(func.lower(User.email) == email.lower())).scalar_one_or_none()


def find_user_by_login(session: Session, identifier: str) -> User | None:
    """Resolve a login identifier that may be a username or an email."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    return get_user_by_username(session, ident) or get_user_by_email(session, ident)


def count_users(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(User)).scalar() or 0)


def count_admins(session: Session, *, exclude_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int(session.execute(stmt).scalar() or 0)


def _unique_username(session: Session, base: str) -> str:
    candidate = base[:60] or "user"
    suffix = 1
    while get_user_by_username(session, candidate) is not None:
        suffix += 1
        candidate = f"{base[:56]}_{suffix}"
    return candidate


def create_user(
    session: Session,
    *,
    username: str,
    password: str | None = None,
    email: str | None = None,
    nickname: str = "",
    role: str = "user",
    status: str = "active",
    avatar_url: str = "",
    commit: bool = True,
) -> User:
    """Create an account. Validates uniqueness and, when given, the password."""
    name = normalize_username(username)
    if name.lower() in RESERVED_USERNAMES and count_users(session) > 0:
        raise AuthError(f"用户名 {name} 为保留名称")
    if get_user_by_username(session, name) is not None:
        raise AuthError(f"用户名 {name} 已存在", code="username_taken", status_code=409)
    mail = normalize_email(email)
    if mail and get_user_by_email(session, mail) is not None:
        raise AuthError(f"邮箱 {mail} 已被使用", code="email_taken", status_code=409)
    if role not in {"admin", "user"}:
        raise AuthError("角色只能是 admin 或 user")
    if status not in {"active", "disabled"}:
        raise AuthError("状态只能是 active 或 disabled")

    password_hash = ""
    if password:
        passwords.validate_password(password)
        password_hash = passwords.hash_password(password)

    # The very first account bootstraps the system, otherwise nobody could ever
    # reach the admin pages once authentication is switched on.
    if settings.auth_first_user_admin and count_users(session) == 0:
        role = "admin"

    user = User(
        username=name,
        email=mail,
        password_hash=password_hash,
        nickname=(nickname or name)[:64],
        avatar_url=avatar_url[:512],
        role=role,
        status=status,
    )
    session.add(user)
    session.flush()  # 需要 user.id 才能建「个人工作区」及其成员关系

    # 每个账号自带一个 personal 工作区。注册时就建好，避免「第一次请求时并发建出
    # 两个空间」这种边角情况；依赖里仍会兜一次，覆盖迁移之前的存量账号。
    from stockmaster.plugins.workspaces.services.service import ensure_personal_workspace

    ensure_personal_workspace(session, user)

    if commit:
        session.commit()
        session.refresh(user)
    log.info("created user %s (role=%s)", user.username, user.role)
    return user


def set_password(session: Session, user: User, password: str, *, revoke_tokens: bool = True) -> None:
    passwords.validate_password(password)
    user.password_hash = passwords.hash_password(password)
    if revoke_tokens:
        user.token_version += 1
    session.commit()
    session.refresh(user)


def touch_login(session: Session, user: User, ip: str) -> None:
    user.last_login_at = utcnow_naive()
    user.last_login_ip = (ip or "")[:64]
    session.commit()


# --------------------------------------------------------------------------- #
# Sessions / tokens
# --------------------------------------------------------------------------- #


def issue_session(user: User) -> LoginResult:
    """Mint an access token for an active user."""
    if not user.is_active:
        raise AuthError("账号已被禁用，请联系管理员", code="user_disabled", status_code=403)
    token, expires_at = tokens.issue_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        token_version=user.token_version,
    )
    return LoginResult(token=token, expires_at=expires_at, user=user)


def authenticate(session: Session, identifier: str, password: str, ip: str = "") -> User:
    """Verify credentials. Same message for unknown user and wrong password."""
    check_throttle(identifier, ip)
    user = find_user_by_login(session, identifier)
    # Always run a hash comparison so response time does not reveal user existence.
    stored = user.password_hash if user else passwords.hash_password("dummy-password-0")
    valid = passwords.verify_password(password, stored)
    if user is None or not valid or not user.password_hash:
        record_failure(identifier, ip)
        audit(session, "login.failed", username=identifier, ip=ip, detail="bad credentials")
        raise AuthError("用户名或密码不正确", code="bad_credentials", status_code=401)
    if not user.is_active:
        record_failure(identifier, ip)
        raise AuthError("账号已被禁用，请联系管理员", code="user_disabled", status_code=403)
    clear_failures(identifier, ip)
    touch_login(session, user, ip)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
        session.commit()
    audit(session, "login.success", user=user, ip=ip)
    return user


def revoke_tokens(session: Session, user: User) -> None:
    """Invalidate every token already issued for this user."""
    user.token_version += 1
    session.commit()
    session.refresh(user)


# --------------------------------------------------------------------------- #
# OAuth state / login tickets
# --------------------------------------------------------------------------- #


def create_flow_state(
    session: Session,
    *,
    kind: str,
    provider: str = "",
    redirect_to: str = "/",
    ttl_seconds: int | None = None,
    user_id: int | None = None,
    payload: dict | None = None,
) -> str:
    ttl = ttl_seconds or (
        settings.auth_oauth_state_ttl_seconds if kind == "oauth" else settings.auth_ticket_ttl_seconds
    )
    state = tokens.random_state()
    session.add(
        AuthFlowState(
            state=state,
            kind=kind,
            provider=provider,
            redirect_to=safe_redirect(redirect_to),
            user_id=user_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            expires_at=utcnow_naive() + timedelta(seconds=ttl),
        )
    )
    session.commit()
    return state


def take_flow_state(session: Session, state: str, *, kind: str) -> AuthFlowState:
    """Consume a state/ticket exactly once, or raise ``FlowStateError``."""
    row = session.get(AuthFlowState, state or "")
    if row is None or row.kind != kind:
        raise FlowStateError()
    if row.consumed_at is not None:
        raise FlowStateError("该登录链接已被使用，请重新登录")
    if row.expires_at < utcnow_naive():
        raise FlowStateError()
    row.consumed_at = utcnow_naive()
    session.commit()
    return row


def purge_flow_states(session: Session, *, older_than_hours: int = 24) -> int:
    """Delete consumed/expired states (bounded housekeeping, safe to call often)."""
    cutoff = utcnow_naive() - timedelta(hours=older_than_hours)
    rows = session.execute(select(AuthFlowState).where(AuthFlowState.created_at < cutoff)).scalars().all()
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# WeChat identities
# --------------------------------------------------------------------------- #


def get_identity(session: Session, provider: str, openid: str) -> UserIdentity | None:
    return session.execute(
        select(UserIdentity).where(UserIdentity.provider == provider, UserIdentity.openid == openid)
    ).scalar_one_or_none()


def list_identities(session: Session, user_id: int) -> list[UserIdentity]:
    return list(
        session.execute(select(UserIdentity).where(UserIdentity.user_id == user_id).order_by(UserIdentity.id)).scalars()
    )


def _username_from_openid(openid: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", openid)[-10:] or "wx"
    return f"wx_{cleaned.lower()}"


def upsert_wechat_user(session: Session, profile: WechatProfile) -> tuple[User, bool, UserIdentity]:
    """Find or create the account behind a WeChat profile.

    Returns ``(user, created, identity)``. Matching order is openid first, then
    unionid: the same person can hold different openids per app, but a common
    unionid, and splitting them would create duplicate accounts.
    """
    identity = get_identity(session, WECHAT_PROVIDER, profile.openid)
    if identity is None and profile.unionid:
        identity = (
            session.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == WECHAT_PROVIDER, UserIdentity.unionid == profile.unionid
                )
            )
            .scalars()
            .first()
        )

    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None:
            raise AuthError("微信绑定关系指向的账号不存在", status_code=500)
        identity.openid = profile.openid
        identity.unionid = profile.unionid or identity.unionid
        identity.nickname = profile.nickname or identity.nickname
        identity.avatar_url = profile.avatar_url or identity.avatar_url
        identity.raw_json = json.dumps(profile.raw, ensure_ascii=False)[:8000]
        session.commit()
        if not user.is_active:
            raise AuthError("账号已被禁用，请联系管理员", code="user_disabled", status_code=403)
        return user, False, identity

    user = create_user(
        session,
        username=_unique_username(session, _username_from_openid(profile.openid)),
        password=None,
        nickname=profile.nickname or "微信用户",
        avatar_url=profile.avatar_url,
        commit=False,
    )
    identity = UserIdentity(
        user_id=user.id,
        provider=WECHAT_PROVIDER,
        openid=profile.openid,
        unionid=profile.unionid,
        nickname=profile.nickname,
        avatar_url=profile.avatar_url,
        raw_json=json.dumps(profile.raw, ensure_ascii=False)[:8000],
    )
    session.add(identity)
    session.commit()
    session.refresh(user)
    session.refresh(identity)
    audit(session, "wechat.register", user=user, detail=f"openid={profile.openid[:24]}")
    return user, True, identity


def bind_wechat(session: Session, user: User, profile: WechatProfile) -> UserIdentity:
    """Attach a WeChat identity to an existing account."""
    existing = get_identity(session, WECHAT_PROVIDER, profile.openid)
    if existing is not None:
        if existing.user_id == user.id:
            return existing
        raise AuthError("该微信已绑定其他账号", code="identity_taken", status_code=409)
    if profile.unionid:
        taken = (
            session.execute(
                select(UserIdentity).where(
                    UserIdentity.provider == WECHAT_PROVIDER,
                    UserIdentity.unionid == profile.unionid,
                    UserIdentity.user_id != user.id,
                )
            )
            .scalars()
            .first()
        )
        if taken is not None:
            raise AuthError("该微信已绑定其他账号", code="identity_taken", status_code=409)

    identity = UserIdentity(
        user_id=user.id,
        provider=WECHAT_PROVIDER,
        openid=profile.openid,
        unionid=profile.unionid,
        nickname=profile.nickname,
        avatar_url=profile.avatar_url,
        raw_json=json.dumps(profile.raw, ensure_ascii=False)[:8000],
    )
    session.add(identity)
    session.commit()
    session.refresh(identity)
    audit(session, "wechat.bind", user=user, detail=f"openid={profile.openid[:24]}")
    return identity


def unbind_identity(session: Session, user: User, identity_id: int) -> None:
    """Remove a binding, refusing to leave the account with no way back in."""
    identity = session.get(UserIdentity, identity_id)
    if identity is None or identity.user_id != user.id:
        raise AuthError("绑定不存在", code="not_found", status_code=404)
    remaining = [i for i in list_identities(session, user.id) if i.id != identity_id]
    if not remaining and not user.password_hash:
        raise AuthError(
            "这是账号唯一的登录方式，请先设置密码再解绑",
            code="last_login_method",
            status_code=400,
        )
    session.delete(identity)
    session.commit()
    audit(session, "wechat.unbind", user=user, detail=f"identity={identity_id}")


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


def audit(
    session: Session,
    action: str,
    *,
    user: User | None = None,
    username: str = "",
    detail: str = "",
    ip: str = "",
    user_agent: str = "",
) -> None:
    """Best-effort audit write: never let logging break the request."""
    try:
        session.add(
            AuthAuditLog(
                user_id=user.id if user else None,
                username=(user.username if user else username)[:64],
                action=action[:64],
                detail=detail[:512],
                ip=(ip or "")[:64],
                user_agent=(user_agent or "")[:256],
            )
        )
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        session.rollback()
        log.debug("audit write failed: %s", exc)


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def bootstrap_admin(session: Session) -> User | None:
    """Create/repair the configured administrator on startup.

    Idempotent: an existing account keeps its password (silently resetting it on
    every boot would be a foot-gun), but its role is promoted to admin so an
    accidentally demoted operator can recover by restarting with the env set.
    """
    username = settings.auth_admin_username.strip()
    password = settings.auth_admin_password
    if not username:
        return None

    user = get_user_by_username(session, username)
    if user is None:
        try:
            created = create_user(
                session,
                username=username,
                password=password or None,
                nickname="管理员",
                role="admin",
                commit=False,
            )
        except AuthError as exc:
            log.warning("bootstrap admin skipped: %s", exc.message)
            session.rollback()
            return None
        session.commit()
        session.refresh(created)
        audit(session, "user.bootstrap", user=created, detail="启动时创建管理员")
        log.info("bootstrap admin %s created", created.username)
        return created

    changed = False
    if user.role != "admin":
        user.role = "admin"
        changed = True
    if password and not user.password_hash:
        user.password_hash = passwords.hash_password(password)
        changed = True
    if changed:
        session.commit()
        log.info("bootstrap admin %s updated", user.username)
    return user
