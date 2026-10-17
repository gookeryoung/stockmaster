"""把「当前工作区 + 我的角色」解析成依赖。

请求如何指明工作区
------------------
数据端点不带路径参数（``/api/v1/watchlist`` 而不是 ``/workspaces/3/watchlist``），
因为绝大多数调用只关心「我现在的上下文」，把 id 塞进每个 URL 会让前端到处拼前缀。
所以当前工作区由请求头 ``X-Workspace-Id`` 决定，缺省落在调用者的 personal 工作区。
这也让「切换工作区」变成前端一次状态变更，而不是全站链接重写。

为什么不复用 ``workspace_id`` 查询参数做唯一来源
--------------------------------------------
下载、上传这类请求已经在用查询参数传自己的东西（文件名、confirm），再混一个同名的
全局参数很容易冲突。查询参数只作为兜底保留，方便 curl / CLI 调试。

未归属桶（``id = 0``）
----------------------
``0`` 不是真实工作区，而是「隔离之前的共享数据」。隔离关闭时所有请求都落在这里，
与升级前逐字节一致；隔离打开时它只能通过 CLI 认领（``workspace claim``）搬出来，
API 不会把任何请求解析到 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from stockmaster.auth.deps import current_user, require_user
from stockmaster.config import settings
from stockmaster.db.models import User, Workspace
from stockmaster.db.session import get_db
from stockmaster.workspace import service
from stockmaster.workspace.roles import WorkspaceRole, at_least, can_manage, can_write

WORKSPACE_HEADER = "X-Workspace-Id"
WORKSPACE_QUERY = "workspace_id"


@dataclass(frozen=True, slots=True)
class WorkspaceCtx:
    """一次请求的归属上下文：哪个工作区、我在这里是什么角色。"""

    id: int
    role: str
    workspace: Workspace | None = None
    user: User | None = None

    @property
    def is_member(self) -> bool:
        return self.workspace is not None

    @property
    def can_write(self) -> bool:
        return can_write(self.role)

    @property
    def can_manage(self) -> bool:
        return can_manage(self.role)


def forbidden(detail: str = "需要所有者权限", code: str = "forbidden") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
        headers={"X-Auth-Error": code},
    )


def _requested_id(request: Request) -> int | None:
    raw = request.headers.get(WORKSPACE_HEADER) or request.query_params.get(WORKSPACE_QUERY)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        # 非法头不要静默忽略：那会让客户端以为自己切到了别的工作区，实际在写
        # personal 工作区。直接 400 更容易定位。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{WORKSPACE_HEADER} 必须是整数",
            headers={"X-Auth-Error": "bad_workspace"},
        ) from None


def _resolve(request: Request, session: Session, user: User | None) -> WorkspaceCtx:
    # 隔离关闭：所有人共享「未归属」桶，等价于引入工作区之前的行为。
    # 角色只用于让写操作继续可用 —— 单机模式下把人降成 viewer 没有任何意义，
    # 因为当时根本没有「成员」这个概念。
    if not settings.data_isolation:
        role = WorkspaceRole.OWNER if (user is not None and user.is_admin) else WorkspaceRole.EDITOR
        return WorkspaceCtx(id=0, role=role, workspace=None, user=user)

    if user is None:
        # 网关关闭（SM_AUTH_ENABLED=false）时的匿名访问：沿用旧语义，写未归属桶。
        # 他们无法管理工作区（那是登录用户的事）。
        return WorkspaceCtx(id=0, role=WorkspaceRole.EDITOR, workspace=None, user=None)

    requested = _requested_id(request)
    if requested is None or requested <= 0:
        workspace = service.ensure_personal_workspace(session, user)
        return WorkspaceCtx(id=workspace.id, role=WorkspaceRole.OWNER, workspace=workspace, user=user)

    workspace = session.get(Workspace, requested)
    if workspace is None:
        # 不存在与「不是成员」返回同一个 404：否则可以靠状态码枚举出工作区 id。
        raise service.not_found()
    role = service.role_of(session, workspace.id, user.id)
    if role is None:
        if user.is_admin:
            # 超管可以代为管理任何工作区（救援用）。管理员的备份/恢复本来就等价于
            # 全量读权限，这里再假装看不见只是把运维逼去改库。
            role = WorkspaceRole.OWNER
        else:
            raise service.not_found()
    return WorkspaceCtx(id=workspace.id, role=role, workspace=workspace, user=user)


def workspace_ctx(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(current_user)],
) -> WorkspaceCtx:
    """解析当前工作区（按请求头，缺省 personal）。同一请求内只算一次。"""
    cached: WorkspaceCtx | None = getattr(request.state, "workspace_ctx", None)
    if cached is not None:
        return cached
    ctx = _resolve(request, session, user)
    request.state.workspace_ctx = ctx
    return ctx


def current_workspace(ctx: Annotated[WorkspaceCtx, Depends(workspace_ctx)]) -> WorkspaceCtx:
    """只读端点用：只要有成员身份就能读。"""
    return ctx


def require_editor(ctx: Annotated[WorkspaceCtx, Depends(workspace_ctx)]) -> WorkspaceCtx:
    """写端点的门槛：viewer 一律 403。

    这里的 403（而不是 404）是对的：调用者已经是该工作区成员，工作区存在这件事
    他本来就看得见，隐藏没有意义，反而让「为什么按钮点了没反应」无从排查。
    """
    if not ctx.can_write:
        raise forbidden("当前工作区为只读权限（viewer），请联系所有者调整", code="read_only")
    return ctx


def path_workspace(
    workspace_id: int,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> WorkspaceCtx:
    """``/workspaces/{id}`` 用：以路径里的 id 为准，忽略请求头。"""
    workspace = service.workspace_or_404(session, workspace_id)
    role = service.role_of(session, workspace.id, user.id)
    if role is None:
        if not user.is_admin:
            raise service.not_found()
        role = WorkspaceRole.OWNER
    return WorkspaceCtx(id=workspace.id, role=role, workspace=workspace, user=user)


def require_workspace_owner(ctx: Annotated[WorkspaceCtx, Depends(path_workspace)]) -> WorkspaceCtx:
    """成员管理与工作区设置的门槛：仅 owner。"""
    if not ctx.can_manage:
        raise forbidden()
    return ctx


def require_minimum(ctx: WorkspaceCtx, minimum: str) -> None:
    """非依赖形式的等级检查（服务层/脚本用）。"""
    if not at_least(ctx.role, minimum):
        raise forbidden()


__all__ = [
    "WORKSPACE_HEADER",
    "WORKSPACE_QUERY",
    "WorkspaceCtx",
    "current_workspace",
    "forbidden",
    "path_workspace",
    "require_editor",
    "require_minimum",
    "require_workspace_owner",
    "workspace_ctx",
]
