"""工作区服务：成员、角色、所有权与「未归属数据」认领。

为什么把判定放在服务层而不是路由里
------------------------------
同一套规则要被三条通道复用：Web API、CLI、以及启动/注册时的自动建档。写在路由
里就只能被 API 用到，CLI 迟早会绕过某条约束（比如「不能移除最后一个所有者」）。
路由只负责把异常翻译成 HTTP 状态码，规则本身集中在这里。

错误约定
--------
抛 ``WorkspaceError(detail, status_code)``：``main.py`` 注册了处理器，把它变成
``{"detail": ...}``；CLI 直接捕获并打印。这样同一段代码不会为了「CLI 不该抛
HTTPException」而写两遍。
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    AlertRule,
    AlertTrigger,
    AlgorithmConfig,
    BacktestRun,
    SampleConfig,
    ScreeningConfig,
    User,
    WatchlistItem,
    Workspace,
    WorkspaceMember,
)
from app.workspace.roles import (
    GRANTABLE_ROLES,
    WorkspaceRole,
    can_manage,
    is_valid_role,
    normalize_role,
)

log = get_logger(__name__)

PERSONAL_KIND = "personal"
SHARED_KIND = "shared"

#: 工作区里的「用户私有数据」表。行情、因子、模型、任务不在此列 —— 它们是全站
#: 共享的参考数据，复制 N 份同样的 K 线没有意义。
#: 顺序有讲究：``algorithm_configs`` 引用 ``sample_configs``，删除/搬运时先处理前者。
DATA_MODELS = (
    WatchlistItem,
    AlertRule,
    AlertTrigger,
    BacktestRun,
    AlgorithmConfig,
    SampleConfig,
    ScreeningConfig,
)

#: 带「工作区内唯一名称」约束的配置表。认领数据时必须特殊处理：直接 UPDATE 会把
#: 两条同名行塞进同一个工作区并撞唯一约束。
CONFIG_MODELS = (ScreeningConfig, SampleConfig, AlgorithmConfig)

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 256


class WorkspaceError(Exception):
    """带 HTTP 语义的领域错误（服务层不依赖 FastAPI）。"""

    def __init__(self, detail: str, status_code: int = 400, code: str = "workspace_error") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code


def not_found(detail: str = "工作区不存在") -> WorkspaceError:
    return WorkspaceError(detail, status_code=404, code="workspace_not_found")


def forbidden(detail: str = "需要所有者权限") -> WorkspaceError:
    return WorkspaceError(detail, status_code=403, code="forbidden")


# --------------------------------------------------------------------------- #
# 个人工作区
# --------------------------------------------------------------------------- #


def personal_workspace(session: Session, user: User) -> Workspace | None:
    """账号的 personal 工作区（不存在时返回 ``None``）。"""
    return session.execute(
        select(Workspace)
        .where(Workspace.kind == PERSONAL_KIND, Workspace.owner_id == user.id)
        .order_by(Workspace.id)
        .limit(1)
    ).scalar_one_or_none()


def ensure_personal_workspace(session: Session, user: User, *, commit: bool = False) -> Workspace:
    """取账号的 personal 工作区，没有就建一个（含 owner 成员关系）。

    注册时会主动调用一次，这里再兜一次是为了覆盖两类账号：迁移之前的存量账号，
    以及注册时事务失败但账号写进去了的边角情况。重复执行是安全的 —— 已有工作区
    只会补成员关系，不会新建。
    """
    workspace = personal_workspace(session, user)
    if workspace is None:
        label = (user.nickname or user.username or "用户").strip()
        workspace = Workspace(
            name=f"{label}的空间",
            kind=PERSONAL_KIND,
            owner_id=user.id,
        )
        session.add(workspace)
        session.flush()  # 需要 workspace.id 才能建成员关系
        log.info("为账号 %s 创建 personal 工作区 #%s", user.username, workspace.id)
    _ensure_membership(session, workspace.id, user.id, WorkspaceRole.OWNER)
    if commit:
        session.commit()
    return workspace


def _ensure_membership(session: Session, workspace_id: int, user_id: int, role: str) -> WorkspaceMember:
    member = member_of(session, workspace_id, user_id)
    if member is None:
        member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
        session.add(member)
        session.flush()
    elif member.role != role:
        member.role = role
    return member


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def workspace_or_404(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise not_found()
    return workspace


def member_of(session: Session, workspace_id: int, user_id: int) -> WorkspaceMember | None:
    return session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    ).scalar_one_or_none()


def role_of(session: Session, workspace_id: int, user_id: int) -> str | None:
    member = member_of(session, workspace_id, user_id)
    return member.role if member is not None else None


def my_workspaces(session: Session, user: User) -> list[tuple[Workspace, str]]:
    """``[(工作区, 我的角色)]``，personal 排在最前，其余按名字。"""
    rows = session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        # kind 升序把 personal 排在前面（p < s），切换器第一项永远是「自己的空间」。
        .order_by(Workspace.kind, Workspace.name, Workspace.id)
    ).all()
    return [(workspace, role) for workspace, role in rows]


def all_workspaces(session: Session) -> list[Workspace]:
    """管理员视角的全部工作区。"""
    return list(session.execute(select(Workspace).order_by(Workspace.id)).scalars())


def member_count(session: Session, workspace_id: int) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        ).scalar_one()
    )


def member_rows(session: Session, workspace_id: int) -> list[tuple[WorkspaceMember, User | None]]:
    rows = session.execute(
        select(WorkspaceMember, User)
        .outerjoin(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.id)
    ).all()
    return [(member, user) for member, user in rows]


def owner_count(session: Session, workspace_id: int) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
            )
        ).scalar_one()
    )


def data_counts(session: Session, workspace_id: int) -> dict[str, int]:
    """工作区里各表的数据量（删除前确认用）。"""
    counts: dict[str, int] = {}
    for model in DATA_MODELS:
        counts[model.__tablename__] = int(
            session.execute(
                select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
            ).scalar_one()
        )
    return counts


# --------------------------------------------------------------------------- #
# 工作区 CRUD
# --------------------------------------------------------------------------- #


def _clean_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise WorkspaceError("工作区名称不能为空")
    if len(value) > MAX_NAME_LENGTH:
        raise WorkspaceError(f"工作区名称最多 {MAX_NAME_LENGTH} 个字符")
    return value


def create_workspace(
    session: Session,
    user: User,
    *,
    name: str,
    description: str = "",
    commit: bool = True,
) -> Workspace:
    """新建共享工作区，创建者成为 owner。"""
    workspace = Workspace(
        name=_clean_name(name),
        description=(description or "").strip()[:MAX_DESCRIPTION_LENGTH],
        kind=SHARED_KIND,
        owner_id=user.id,
    )
    session.add(workspace)
    session.flush()
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))
    if commit:
        session.commit()
    return workspace


def update_workspace(
    session: Session,
    workspace: Workspace,
    *,
    name: str | None = None,
    description: str | None = None,
    commit: bool = True,
) -> Workspace:
    if name is not None:
        workspace.name = _clean_name(name)
    if description is not None:
        workspace.description = description.strip()[:MAX_DESCRIPTION_LENGTH]
    if commit:
        session.commit()
    return workspace


def delete_workspace(session: Session, workspace: Workspace, *, purge: bool = False) -> dict[str, int]:
    """删除工作区。

    personal 工作区不允许删除：它是账号数据的落脚点，删掉之后工作区里的行会变成
    「指向一个不存在的工作区」，既看不见也无法再通过界面访问。

    未清空数据时要求显式 ``purge``：数据行没有外键级联（``workspace_id = 0`` 是合法
    的「未归属」值），静默删工作区会留下一堆孤儿行。
    """
    if workspace.kind == PERSONAL_KIND:
        raise WorkspaceError("个人工作区不能删除（可以改名或转让）", status_code=400, code="personal_workspace")
    counts = data_counts(session, workspace.id)
    total = sum(counts.values())
    if total and not purge:
        raise WorkspaceError(
            f"工作区还有 {total} 条数据（{_format_counts(counts)}），确认删除请传 purge=true",
            status_code=409,
            code="workspace_not_empty",
        )
    if total:
        for model in DATA_MODELS:
            session.execute(model.__table__.delete().where(model.workspace_id == workspace.id))
    session.execute(WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == workspace.id))
    session.delete(workspace)
    session.commit()
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    labels = {
        "watchlist_items": "自选",
        "alert_rules": "规则",
        "alert_triggers": "预警",
        "backtest_runs": "回测",
        "screening_configs": "选股配置",
        "sample_configs": "样本配置",
        "algorithm_configs": "算法配置",
    }
    return "、".join(f"{labels.get(k, k)} {v}" for k, v in counts.items() if v)


# --------------------------------------------------------------------------- #
# 成员与角色
# --------------------------------------------------------------------------- #


def add_member(
    session: Session,
    workspace: Workspace,
    *,
    username: str,
    role: str = WorkspaceRole.VIEWER,
    commit: bool = True,
) -> WorkspaceMember:
    """按用户名加成员。只允许授予 editor/viewer（owner 走转让）。"""
    if not is_valid_role(role):
        raise WorkspaceError(f"未知角色：{role}")
    normalized = normalize_role(role)
    if normalized not in GRANTABLE_ROLES:
        raise WorkspaceError(f"只能授予 {' / '.join(GRANTABLE_ROLES)}；所有者请用「转让所有权」")

    user = session.execute(select(User).where(User.username == (username or "").strip())).scalar_one_or_none()
    if user is None:
        raise WorkspaceError(f"用户不存在：{username}", status_code=404, code="user_not_found")
    if not user.is_active:
        raise WorkspaceError(f"账号 {user.username} 已被禁用", status_code=400, code="user_disabled")
    if member_of(session, workspace.id, user.id) is not None:
        raise WorkspaceError(f"{user.username} 已经是该工作区成员", status_code=409, code="already_member")

    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=normalized)
    session.add(member)
    if commit:
        session.commit()
    return member


def set_member_role(
    session: Session,
    workspace: Workspace,
    *,
    user_id: int,
    role: str,
    commit: bool = True,
) -> WorkspaceMember:
    """改成员角色（仅 owner 可调用；由路由的依赖保证）。"""
    if not is_valid_role(role):
        raise WorkspaceError(f"未知角色：{role}")
    member = member_of(session, workspace.id, user_id)
    if member is None:
        raise not_found("成员不存在")

    target = normalize_role(role)
    if member.role == WorkspaceRole.OWNER and target != WorkspaceRole.OWNER:
        _guard_last_owner(session, workspace.id)
    member.role = target
    if commit:
        session.commit()
    return member


def remove_member(session: Session, workspace: Workspace, *, user_id: int, commit: bool = True) -> None:
    member = member_of(session, workspace.id, user_id)
    if member is None:
        raise not_found("成员不存在")
    if member.role == WorkspaceRole.OWNER:
        _guard_last_owner(session, workspace.id)
    session.delete(member)
    if commit:
        session.commit()


def transfer_owner(session: Session, workspace: Workspace, *, user_id: int, commit: bool = True) -> WorkspaceMember:
    """把所有权交给某个成员，原所有者降为 editor。

    「降级」而不是「移出」：转让之后原所有者通常还要继续用这套数据，直接踢掉等于
    用一个误操作把人的工作区弄丢。
    """
    target = member_of(session, workspace.id, user_id)
    if target is None:
        raise WorkspaceError("目标用户不是该工作区成员", status_code=400, code="not_member")
    if target.role == WorkspaceRole.OWNER:
        return target

    for member in session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
        )
    ).scalars():
        member.role = WorkspaceRole.EDITOR
    target.role = WorkspaceRole.OWNER
    workspace.owner_id = target.user_id
    if commit:
        session.commit()
    return target


def _guard_last_owner(session: Session, workspace_id: int) -> None:
    if owner_count(session, workspace_id) <= 1:
        raise WorkspaceError(
            "工作区至少保留一名所有者；请先转让所有权",
            status_code=400,
            code="last_owner",
        )


def require_manage(role: str | None) -> None:
    """在服务层再拦一次（路由依赖是第一道，CLI/脚本是第二道）。"""
    if not can_manage(role):
        raise forbidden()


# --------------------------------------------------------------------------- #
# 未归属数据认领
# --------------------------------------------------------------------------- #


def claim_unassigned(
    session: Session,
    workspace_id: int,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, int]:
    """把 ``workspace_id = 0`` 的历史数据搬进指定工作区。

    为什么需要它
    ------------
    隔离上线前写入的自选股、预警规则、回测历史全都是 ``workspace_id = 0``。开启隔离
    之后登录用户看不到这些数据 —— 它们不是丢了，而是留在「未归属」桶里。

    为什么不直接 ``UPDATE ... SET workspace_id = ?``
    -----------------------------------------------
    ``watchlist_items`` 的唯一约束是 ``(workspace_id, list_name, code)``，三张策略配置
    表还有 ``(workspace_id, name)``。如果目标工作区已经有同键数据，直接搬会撞约束。
    这里对冲突行只删「旧的无主副本」，保留工作区自己那份（连同它自己的 note / 参数），
    并如实报告跳过数量。

    普通表单条 UPDATE；配置表必须逐行搬：名称冲突要按「先到先得」保留目标工作区那份。
    """
    workspace_or_404(session, workspace_id)

    def clause(model):
        return model.id.isnot(None) if overwrite else model.workspace_id == 0

    watch_stmt = select(WatchlistItem).where(clause(WatchlistItem))
    watch_rows = list(session.execute(watch_stmt).scalars())
    if overwrite:
        watch_rows = [row for row in watch_rows if row.workspace_id != workspace_id]

    mine = {
        (row.list_name, row.code)
        for row in session.execute(select(WatchlistItem).where(WatchlistItem.workspace_id == workspace_id)).scalars()
    }
    conflicts = [row for row in watch_rows if (row.list_name, row.code) in mine]
    movable = [row for row in watch_rows if (row.list_name, row.code) not in mine]

    counts: dict[str, int] = {"watchlist_items": len(movable)}
    for model in (AlertRule, AlertTrigger, BacktestRun):
        counts[model.__tablename__] = int(
            session.execute(select(func.count()).select_from(model).where(clause(model))).scalar_one()
        )
    counts["duplicates_skipped"] = len(conflicts)

    # 配置表：先算出每张表能搬多少行（同名冲突的旧副本会被删掉）。
    config_movable: dict[type, list] = {}
    config_conflicts: dict[type, list] = {}
    for model in CONFIG_MODELS:
        target_names = set(session.execute(select(model.name).where(model.workspace_id == workspace_id)).scalars())
        rows = list(session.execute(select(model).where(clause(model))).scalars())
        if overwrite:
            rows = [row for row in rows if row.workspace_id != workspace_id]
        movable_rows: list = []
        conflicting_rows: list = []
        used = set(target_names)
        for row in rows:
            if row.name in used:
                conflicting_rows.append(row)
                continue
            used.add(row.name)
            movable_rows.append(row)
        config_movable[model] = movable_rows
        config_conflicts[model] = conflicting_rows
        counts[model.__tablename__] = len(movable_rows)
        counts["duplicates_skipped"] += len(conflicting_rows)

    if dry_run:
        return counts

    for row in conflicts:
        session.delete(row)
    for row in movable:
        row.workspace_id = workspace_id
    for model in (AlertRule, AlertTrigger, BacktestRun):
        session.execute(update(model).where(clause(model)).values(workspace_id=workspace_id))
    # 配置表逐行改归属：同名冲突的旧副本删除，其余搬进目标工作区。
    for rows in config_conflicts.values():
        for row in rows:
            session.delete(row)
    for rows in config_movable.values():
        for row in rows:
            row.workspace_id = workspace_id
    session.commit()
    return counts


__all__ = [
    "CONFIG_MODELS",
    "DATA_MODELS",
    "PERSONAL_KIND",
    "SHARED_KIND",
    "WorkspaceError",
    "add_member",
    "all_workspaces",
    "claim_unassigned",
    "create_workspace",
    "data_counts",
    "delete_workspace",
    "ensure_personal_workspace",
    "forbidden",
    "member_count",
    "member_of",
    "member_rows",
    "my_workspaces",
    "not_found",
    "owner_count",
    "personal_workspace",
    "remove_member",
    "require_manage",
    "role_of",
    "set_member_role",
    "transfer_owner",
    "update_workspace",
    "workspace_or_404",
]
