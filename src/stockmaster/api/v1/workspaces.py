"""工作区与成员管理端点（参考 cndb 的 workspaces 插件）。

为什么成员管理单独成一组端点
--------------------------
工作区是**数据的归属单位**，成员关系决定「谁能看见、谁能改」。把成员管理混进
``/users`` 会让「账号」和「权限」两件事纠缠在一起：账号是全局的，权限是相对于某个
工作区的。``/workspaces/{id}/members`` 这条路径本身就表达了「相对于」。

权限约定
--------
* 列表 / 详情 / 成员列表：工作区成员（非成员一律 404，不泄露工作区是否存在）
* 改名字、加删成员、改角色、转让、删除：仅 owner
* 「最后一个 owner 不能被降级/移除」由服务层强制 —— 这条规则一旦破掉，工作区就
  再也没人能管理，只能改库。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.api.deps import db_session
from stockmaster.auth.deps import require_user
from stockmaster.db.models import User
from stockmaster.schemas.workspace import (
    MemberIn,
    MemberRoleIn,
    TransferIn,
    WorkspaceDeleted,
    WorkspaceIn,
    WorkspaceMemberOut,
    WorkspaceOut,
    WorkspaceUpdate,
)
from stockmaster.workspace import service
from stockmaster.workspace.deps import WorkspaceCtx, path_workspace, require_workspace_owner

router = APIRouter(tags=["workspaces"])


def _out(session: Session, workspace, role: str) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        kind=workspace.kind,
        owner_id=workspace.owner_id,
        role=role,
        member_count=service.member_count(session, workspace.id),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _member_out(member, user: User | None) -> WorkspaceMemberOut:
    return WorkspaceMemberOut(
        user_id=member.user_id,
        username=user.username if user is not None else "",
        nickname=(user.nickname if user is not None else "") or "",
        avatar_url=(user.avatar_url if user is not None else "") or "",
        role=member.role,
        joined_at=member.created_at,
    )


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(
    session: Annotated[Session, Depends(db_session)],
    user: Annotated[User, Depends(require_user)],
    all_: bool = Query(default=False, alias="all", description="管理员查看全部工作区"),
):
    """我参与的工作区（``?all=true`` 时管理员可列出全部）。"""
    if all_ and user.is_admin:
        return [
            _out(session, workspace, service.role_of(session, workspace.id, user.id) or "owner")
            for workspace in service.all_workspaces(session)
        ]
    return [_out(session, workspace, role) for workspace, role in service.my_workspaces(session, user)]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceIn,
    session: Annotated[Session, Depends(db_session)],
    user: Annotated[User, Depends(require_user)],
):
    """新建共享工作区，创建者成为 owner。"""
    workspace = service.create_workspace(session, user, name=payload.name, description=payload.description)
    return _out(session, workspace, "owner")


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)], session: Annotated[Session, Depends(db_session)]
):
    """工作区详情（成员可见）。"""
    return _out(session, ctx.workspace, ctx.role)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    payload: WorkspaceUpdate,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """改名 / 改描述（仅 owner）。"""
    workspace = service.update_workspace(
        session,
        ctx.workspace,
        name=payload.name,
        description=payload.description,
    )
    return _out(session, workspace, ctx.role)


@router.delete("/{workspace_id}", response_model=WorkspaceDeleted)
def delete_workspace(
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
    purge: bool = Query(default=False, description="工作区非空时必须显式确认"),
):
    """删除工作区（仅 owner；personal 工作区不可删除）。"""
    purged = service.delete_workspace(session, ctx.workspace, purge=purge)
    return WorkspaceDeleted(status="deleted", purged=purged)


# --------------------------------------------------------------------------- #
# 成员
# --------------------------------------------------------------------------- #


@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberOut])
def list_members(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)],
    session: Annotated[Session, Depends(db_session)],
):
    """成员列表（成员可见 —— 知道「谁能看这份数据」是协作的基本信息）。"""
    return [_member_out(member, user) for member, user in service.member_rows(session, ctx.id)]


@router.get("/{workspace_id}/members/candidates", response_model=list[WorkspaceMemberOut])
def list_candidates(
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
    search: str = Query(default="", description="按用户名/昵称过滤"),
):
    """可加入的候选账号（仅 owner，已排除现有成员）。"""
    member_ids = [member.user_id for member, _ in service.member_rows(session, ctx.id)]
    stmt = select(User).where(User.status == "active")
    if member_ids:
        stmt = stmt.where(User.id.not_in(member_ids))
    keyword = search.strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where((User.username.ilike(like)) | (User.nickname.ilike(like)))
    users = session.execute(stmt.order_by(User.username).limit(50)).scalars().all()
    # 复用成员结构：候选者还没有角色，统一回 viewer，前端只拿 id/名字。
    return [
        WorkspaceMemberOut(user_id=u.id, username=u.username, nickname=u.nickname or "", avatar_url=u.avatar_url or "")
        for u in users
        if u.id not in set(member_ids)
    ]


@router.post("/{workspace_id}/members", response_model=WorkspaceMemberOut, status_code=status.HTTP_201_CREATED)
def add_member(
    payload: MemberIn,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """按用户名加成员（仅 owner；只可授予 editor / viewer）。"""
    member = service.add_member(session, ctx.workspace, username=payload.username, role=payload.role)
    user = session.get(User, member.user_id)
    return _member_out(member, user)


@router.patch("/{workspace_id}/members/{user_id}", response_model=WorkspaceMemberOut)
def update_member_role(
    user_id: int,
    payload: MemberRoleIn,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """修改成员角色（仅 owner；最后一个 owner 不可降级）。"""
    member = service.set_member_role(session, ctx.workspace, user_id=user_id, role=payload.role)
    return _member_out(member, session.get(User, member.user_id))


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    user_id: int,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """移除成员（仅 owner；最后一个 owner 不可移除）。"""
    service.remove_member(session, ctx.workspace, user_id=user_id)


@router.post("/{workspace_id}/transfer", response_model=WorkspaceMemberOut)
def transfer_owner(
    payload: TransferIn,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """转让所有权（仅 owner；原 owner 降为 editor）。"""
    member = service.transfer_owner(session, ctx.workspace, user_id=payload.user_id)
    return _member_out(member, session.get(User, member.user_id))
