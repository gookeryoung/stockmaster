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

from stockmaster.core import quota
from stockmaster.core.auth_deps import require_user
from stockmaster.core.deps import db_session
from stockmaster.core.logging import get_logger
from stockmaster.core.workspace_deps import WorkspaceCtx, path_workspace, require_workspace_owner
from stockmaster.db.models import User
from stockmaster.plugins.workspaces.schemas.workspace import (
    ChannelIn,
    ChannelUpdate,
    MemberIn,
    MemberRoleIn,
    TransferIn,
    WorkspaceChannelOut,
    WorkspaceDeleted,
    WorkspaceIn,
    WorkspaceMemberOut,
    WorkspaceOut,
    WorkspaceUpdate,
    WorkspaceUsageOut,
)
from stockmaster.plugins.workspaces.services import service

router = APIRouter(tags=["workspaces"])

log = get_logger(__name__)


def _out(session: Session, workspace, role: str, *, data_total: int | None = None) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        kind=workspace.kind,
        owner_id=workspace.owner_id,
        role=role,
        member_count=service.member_count(session, workspace.id),
        # 列表页会批量算好传进来；详情/创建等单条路径由这里兜底。
        data_total=quota.data_total(session, workspace.id) if data_total is None else data_total,
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


def _channel_out(channel) -> WorkspaceChannelOut:
    """对外只给掩码 URL：明文 token 不离开服务端。"""
    return WorkspaceChannelOut(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        url=service.mask_url(channel.url),
        enabled=channel.enabled,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
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
        rows = [
            (workspace, service.role_of(session, workspace.id, user.id) or "owner")
            for workspace in service.all_workspaces(session)
        ]
    else:
        rows = service.my_workspaces(session, user)
    # 数据量按 6 张表各一次 GROUP BY 批量算：列表 N 个工作区逐个算会退化成 N×6 次查询。
    totals = quota.data_totals(session, [workspace.id for workspace, _role in rows])
    return [_out(session, workspace, role, data_total=totals.get(workspace.id, 0)) for workspace, role in rows]


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
# 用量与配额
# --------------------------------------------------------------------------- #


@router.get("/{workspace_id}/stats", response_model=WorkspaceUsageOut)
def workspace_stats(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)],
    session: Annotated[Session, Depends(db_session)],
):
    """工作区用量与配额（成员可读，只读无副作用）。

    配额只是治理手段：``limits`` 里 ``0`` 表示不限，前端据此展示「不限」而不是
    「0 条」。隔离关闭时没有工作区语义，这个端点仍按路径 id 回答，但不会限制写路径。
    """
    current = quota.usage(session, ctx.id)
    return WorkspaceUsageOut(
        workspace_id=ctx.id,
        usage=current,
        limits=quota.limits(),
        labels=quota.USAGE_LABELS,
        total=sum(current.values()),
    )


# --------------------------------------------------------------------------- #
# 通知通道
# --------------------------------------------------------------------------- #


@router.get("/{workspace_id}/channels", response_model=list[WorkspaceChannelOut])
def list_channels(
    ctx: Annotated[WorkspaceCtx, Depends(path_workspace)],
    session: Annotated[Session, Depends(db_session)],
):
    """通道列表（成员可见；URL 一律掩码）。"""
    return [_channel_out(channel) for channel in service.channels(session, ctx.id)]


@router.post(
    "/{workspace_id}/channels",
    response_model=WorkspaceChannelOut,
    status_code=status.HTTP_201_CREATED,
)
def create_channel(
    payload: ChannelIn,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """新建通道（仅 owner —— 通道是数据出口，不随「可写数据」下放给 editor）。"""
    quota.enforce(session, ctx.id, "channels")
    channel = service.create_channel(
        session,
        ctx.workspace,
        name=payload.name,
        kind=payload.kind,
        url=payload.url,
        enabled=payload.enabled,
        created_by=ctx.user.id if ctx.user is not None else None,
    )
    return _channel_out(channel)


@router.patch("/{workspace_id}/channels/{channel_id}", response_model=WorkspaceChannelOut)
def update_channel(
    channel_id: int,
    payload: ChannelUpdate,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """改名 / 改类型 / 改地址 / 启停（仅 owner；省略 url 表示保持原值）。"""
    channel = service.channel_or_404(session, ctx.id, channel_id)
    channel = service.update_channel(
        session,
        ctx.workspace,
        channel,
        name=payload.name,
        kind=payload.kind,
        url=payload.url,
        enabled=payload.enabled,
    )
    return _channel_out(channel)


@router.delete("/{workspace_id}/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: int,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """删除通道（仅 owner）。"""
    channel = service.channel_or_404(session, ctx.id, channel_id)
    service.delete_channel(session, ctx.workspace, channel)


@router.post("/{workspace_id}/channels/{channel_id}/test")
def test_channel(
    channel_id: int,
    ctx: Annotated[WorkspaceCtx, Depends(require_workspace_owner)],
    session: Annotated[Session, Depends(db_session)],
):
    """给通道发一条测试消息（仅 owner）。网络失败返回 ``delivered=false``，绝不 500。"""
    channel = service.channel_or_404(session, ctx.id, channel_id)
    try:
        # 惰性导入：投递实现属于 alerts 插件。放在函数内既避免工作区插件对投递实现的
        # 启动期依赖，也让两个插件可以各自独立演进（测试里也是 monkeypatch 这个符号）。
        from stockmaster.plugins.alerts.services.channels import send_test
    except Exception:
        log.warning("通知投递模块不可用，通道 #%s 的测试按未送达处理", channel.id)
        delivered = False
    else:
        try:
            delivered = bool(send_test(channel))
        except Exception as exc:
            # 配错地址、网络不通都是「预期内的失败」，接口只回报结果，不抛 500。
            log.warning("通道 #%s 测试投递失败：%s", channel.id, exc)
            delivered = False
    return {"delivered": delivered, "channel": channel.name, "kind": channel.kind}


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
    quota.enforce(session, ctx.id, "members")
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
