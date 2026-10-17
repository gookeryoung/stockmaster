"""Administrator endpoints for account management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from stockmaster.api.deps import db_session
from stockmaster.api.v1.auth import as_utc
from stockmaster.api.v1.auth import user_out as _user_out
from stockmaster.auth import passwords, service
from stockmaster.auth.deps import client_agent, client_ip, require_admin
from stockmaster.core.logging import get_logger
from stockmaster.db.models import AuthAuditLog, User
from stockmaster.schemas.auth import (
    AuditLogOut,
    PasswordResetIn,
    UserCreateIn,
    UserOut,
    UserPage,
    UserUpdateIn,
)

log = get_logger(__name__)
# Router-level guard: every account-management endpoint is administrator-only,
# including ones added later.
router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])

SessionDep = Annotated[Session, Depends(db_session)]
AdminDep = Annotated[User, Depends(require_admin)]


def _out(session: Session, user: User) -> UserOut:
    return _user_out(session, user, with_identities=True)


def _require_other_admin(session: Session, target: User, acting: User) -> None:
    """Guard the two ways an installation can lock itself out of its own admin.

    Demoting/disabling yourself is allowed (with another admin present); removing
    the last active administrator or yourself entirely is not.
    """
    if target.id == acting.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能修改自己的角色或状态，请让其他管理员操作",
        )
    if target.role == "admin" and service.count_admins(session, exclude_id=target.id) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="至少需要保留一名启用的管理员",
        )


@router.get("", response_model=UserPage)
def list_users(
    session: SessionDep,
    q: str = Query(default="", max_length=64, description="按用户名/昵称/邮箱模糊搜索"),
    role: str | None = Query(default=None, pattern="^(admin|user)$"),
    user_status: str | None = Query(default=None, alias="status", pattern="^(active|disabled)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    stmt = select(User)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.nickname.ilike(like), User.email.ilike(like)))
    if role:
        stmt = stmt.where(User.role == role)
    if user_status:
        stmt = stmt.where(User.status == user_status)

    total = int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)
    rows = session.execute(stmt.order_by(User.id).offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return UserPage(total=total, items=[_out(session, row) for row in rows])


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreateIn, request: Request, session: SessionDep, acting: AdminDep):
    try:
        user = service.create_user(
            session,
            username=payload.username,
            password=payload.password,
            email=payload.email,
            nickname=payload.nickname,
            role=payload.role,
            status=payload.status,
        )
    except service.AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    service.audit(
        session,
        "user.create",
        user=acting if acting.id else None,
        username=acting.username,
        detail=f"created {user.username}",
        ip=client_ip(request),
        user_agent=client_agent(request),
    )
    return _out(session, user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    request: Request,
    session: SessionDep,
    acting: AdminDep,
):
    target = _get(session, user_id)

    changing_privilege = (payload.role is not None and payload.role != target.role) or (
        payload.status is not None and payload.status != target.status
    )
    if changing_privilege:
        _require_other_admin(session, target, acting)

    if payload.nickname is not None:
        target.nickname = payload.nickname.strip()[:64]
    if payload.email is not None:
        email = service.normalize_email(payload.email)
        if email and email != (target.email or ""):
            other = service.get_user_by_email(session, email)
            if other is not None and other.id != target.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已被使用")
        target.email = email
    if payload.role is not None:
        target.role = payload.role
    if payload.status is not None:
        target.status = payload.status
        if payload.status == "disabled":
            # Kick existing sessions immediately instead of waiting for expiry.
            target.token_version += 1

    session.commit()
    session.refresh(target)
    service.audit(
        session,
        "user.update",
        user=acting if acting.id else None,
        username=acting.username,
        detail=f"{target.username} role={target.role} status={target.status}",
        ip=client_ip(request),
    )
    return _out(session, target)


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: int,
    payload: PasswordResetIn,
    request: Request,
    session: SessionDep,
    acting: AdminDep,
):
    target = _get(session, user_id)
    try:
        passwords.validate_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    service.set_password(session, target, payload.new_password)
    service.audit(
        session,
        "user.password_reset",
        user=acting if acting.id else None,
        username=acting.username,
        detail=f"reset password for {target.username}",
        ip=client_ip(request),
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, request: Request, session: SessionDep, acting: AdminDep):
    target = _get(session, user_id)
    _require_other_admin(session, target, acting)
    username = target.username
    session.delete(target)
    session.commit()
    service.audit(
        session,
        "user.delete",
        user=acting if acting.id else None,
        username=acting.username,
        detail=f"deleted {username}",
        ip=client_ip(request),
    )


@router.get("/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = Query(default=None, max_length=64),
):
    stmt = select(AuthAuditLog)
    if action:
        stmt = stmt.where(AuthAuditLog.action == action)
    rows = session.execute(stmt.order_by(AuthAuditLog.id.desc()).limit(limit)).scalars().all()
    return [
        AuditLogOut(
            id=row.id,
            user_id=row.user_id,
            username=row.username,
            action=row.action,
            detail=row.detail,
            ip=row.ip,
            created_at=as_utc(row.created_at),
        )
        for row in rows
    ]


def _get(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"用户 {user_id} 不存在")
    return user
