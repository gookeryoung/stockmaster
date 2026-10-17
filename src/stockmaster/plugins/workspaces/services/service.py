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

import re
from string import Formatter
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockmaster.core.logging import get_logger
from stockmaster.db.models import (
    AlertRule,
    AlertTrigger,
    AlgorithmConfig,
    BacktestRun,
    SampleConfig,
    ScreeningConfig,
    User,
    WatchlistItem,
    Workspace,
    WorkspaceAlertPolicy,
    WorkspaceChannel,
    WorkspaceMember,
)
from stockmaster.plugins.workspaces.services.roles import (
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

#: 工作区级通知通道支持的投递类型。webhook 是通用 JSON POST（钉钉/企微/Server酱
#: 中转），bark 走 iOS 推送，email 走部署级 SMTP；新增类型时
#: ``alerts.services.channels.build_channel`` 必须同步支持，否则通道能建出来却发不出去。
CHANNEL_KINDS = ("webhook", "bark", "email")
MAX_CHANNEL_NAME_LENGTH = 64
MAX_CHANNEL_URL_LENGTH = 512
MAX_EMAIL_RECIPIENTS = 10
#: 简单邮箱校验：只排除明显非法的输入（无 @、空白、缺域名点），不做 RFC 5322 全集。
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: 短于这个长度的 URL 去掉主机名后没有可展示的信息，掩码直接整体打码。
_CHANNEL_MASK_MIN_LENGTH = 16

MAX_MESSAGE_TEMPLATE_LENGTH = 2000
#: 消息模板允许的占位符（与 ``alerts.services.channels`` 的 format 调用一致）。
_TEMPLATE_FIELDS = frozenset({"title", "body", "code", "rule_id", "level"})


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


def _clean_message_template(template: str) -> str:
    """校验工作区预警消息模板；空串 = 清除（跟随默认标题/正文）。

    用 ``string.Formatter().parse`` 静态校验占位符：只允许 ``{title}`` 等五个字段，
    但允许格式说明符（``{title:>10}``）和转换符（``{body!r}``）。非法花括号与未知
    字段写库前就 422，避免运行期才发现模板永远发不出去。
    """
    value = (template or "").strip()
    if not value:
        return ""
    if len(value) > MAX_MESSAGE_TEMPLATE_LENGTH:
        raise WorkspaceError(
            f"消息模板最多 {MAX_MESSAGE_TEMPLATE_LENGTH} 个字符", status_code=422, code="invalid_template"
        )
    try:
        parsed = list(Formatter().parse(value))
    except ValueError as exc:  # 单个 { 或 } → ValueError
        raise WorkspaceError(
            "消息模板的占位符不合法（花括号未闭合）", status_code=422, code="invalid_template"
        ) from exc

    for _literal, field_name, _format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in _TEMPLATE_FIELDS:
            options = " ".join(f"{{{field}}}" for field in sorted(_TEMPLATE_FIELDS))
            raise WorkspaceError(
                f"消息模板只支持 {options}，不支持：{{{field_name}}}",
                status_code=422,
                code="invalid_template",
            )
        if conversion not in (None, "r", "s", "a"):
            raise WorkspaceError(
                f"消息模板的转换符不支持：!{conversion}（只允许 !r / !s / !a）",
                status_code=422,
                code="invalid_template",
            )
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
    message_template: str | None = None,
    commit: bool = True,
) -> Workspace:
    if name is not None:
        workspace.name = _clean_name(name)
    if description is not None:
        workspace.description = description.strip()[:MAX_DESCRIPTION_LENGTH]
    if message_template is not None:
        workspace.message_template = _clean_message_template(message_template)
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
    # 通道不是「数据」，不计入 data_counts（否则删除非空工作区会突然多出计数、
    # 破坏既有 purge 语义与计数测试），但它必须随工作区一起消失。FK 上虽然写了
    # ON DELETE CASCADE，SQLite 默认并不开启外键约束，所以这里显式删除。
    session.execute(WorkspaceChannel.__table__.delete().where(WorkspaceChannel.workspace_id == workspace.id))
    # 预警参数策略同理：它是工作区配置而不是业务数据，不该触发 purge 确认，但也不能
    # 留成指向已删工作区的孤儿行。
    session.execute(WorkspaceAlertPolicy.__table__.delete().where(WorkspaceAlertPolicy.workspace_id == workspace.id))
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
# 工作区级通知通道
# --------------------------------------------------------------------------- #


def channels(session: Session, workspace_id: int) -> list[WorkspaceChannel]:
    """工作区的通知通道，按创建顺序（id 升序）返回。"""
    return list(
        session.execute(
            select(WorkspaceChannel).where(WorkspaceChannel.workspace_id == workspace_id).order_by(WorkspaceChannel.id)
        ).scalars()
    )


def channel_or_404(session: Session, workspace_id: int, channel_id: int) -> WorkspaceChannel:
    """取通道；不存在与「属于别的工作区」都返回 404。

    不区分两种情况的理由与工作区本身一致：能靠状态码判断出「这个 id 存在，只是不在
    你的工作区」就等于泄露了别的租户有多少条通道。
    """
    channel = session.get(WorkspaceChannel, channel_id)
    if channel is None or channel.workspace_id != workspace_id:
        raise not_found("通知通道不存在")
    return channel


def _clean_channel_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise WorkspaceError("通道名称不能为空", status_code=422, code="invalid_channel")
    if len(value) > MAX_CHANNEL_NAME_LENGTH:
        raise WorkspaceError(f"通道名称最多 {MAX_CHANNEL_NAME_LENGTH} 个字符", status_code=422, code="invalid_channel")
    return value


def _clean_channel_kind(kind: str) -> str:
    value = (kind or "").strip().lower()
    if value not in CHANNEL_KINDS:
        options = " / ".join(CHANNEL_KINDS)
        raise WorkspaceError(f"不支持的通道类型：{kind}（可选 {options}）", status_code=422, code="invalid_channel")
    return value


def _clean_channel_url(url: str) -> str:
    """校验外发地址：非空、http(s)、无空白/控制字符。

    这里不检查主机是否存在、也不允许 ``file:`` / ``ftp:`` 之类的 scheme —— 通道地址
    会被服务端主动请求，限制成 http(s) 是防止「配置一个通道就等于让服务器访问任意
    本地资源」的最小防线。
    """
    value = (url or "").strip()
    if not value:
        raise WorkspaceError("通道地址不能为空", status_code=422, code="invalid_channel")
    if len(value) > MAX_CHANNEL_URL_LENGTH:
        raise WorkspaceError(f"通道地址最多 {MAX_CHANNEL_URL_LENGTH} 个字符", status_code=422, code="invalid_channel")
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise WorkspaceError("通道地址不能包含空白或控制字符", status_code=422, code="invalid_channel")
    try:
        parts = urlsplit(value)
    except ValueError as exc:  # 例如非法的 IPv6 字面量
        raise WorkspaceError("通道地址不是合法的 URL", status_code=422, code="invalid_channel") from exc
    if (parts.scheme or "").lower() not in ("http", "https") or not parts.netloc:
        raise WorkspaceError("通道地址必须是 http(s):// 开头的完整 URL", status_code=422, code="invalid_channel")
    return value


def _clean_channel_target(kind: str, target: str) -> str:
    """按通道类型校验「目标」：webhook/bark 是 URL，email 是收件人串。"""
    if kind == "email":
        return _clean_email_recipients(target)
    return _clean_channel_url(target)


def _clean_email_recipients(target: str) -> str:
    """校验并规范化邮件收件人：逗号/分号分隔、最多 10 个、总长 ≤512。

    返回 ``"a@x.com, b@y.com"`` 形式的规范串——分号统一成逗号、去掉多余空白，
    入库后投递端（``EmailChannel``）直接按逗号拆分即可。
    """
    value = (target or "").strip()
    if not value:
        raise WorkspaceError("邮件收件人不能为空", status_code=422, code="invalid_channel")
    if len(value) > MAX_CHANNEL_URL_LENGTH:
        raise WorkspaceError(f"邮件收件人最多 {MAX_CHANNEL_URL_LENGTH} 个字符", status_code=422, code="invalid_channel")
    recipients = [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    if not recipients:
        raise WorkspaceError("邮件收件人不能为空", status_code=422, code="invalid_channel")
    if len(recipients) > MAX_EMAIL_RECIPIENTS:
        raise WorkspaceError(f"邮件收件人最多 {MAX_EMAIL_RECIPIENTS} 个", status_code=422, code="invalid_channel")
    for recipient in recipients:
        if not _EMAIL_PATTERN.match(recipient):
            raise WorkspaceError(f"邮件地址不合法：{recipient}", status_code=422, code="invalid_channel")
    normalized = ", ".join(recipients)
    if len(normalized) > MAX_CHANNEL_URL_LENGTH:
        raise WorkspaceError(f"邮件收件人最多 {MAX_CHANNEL_URL_LENGTH} 个字符", status_code=422, code="invalid_channel")
    return normalized


def mask_url(url: str) -> str:
    """把含凭据的 URL 变成可展示的掩码：``scheme://主机/****最后4位``。

    只保留 scheme 与主机名（成员要靠它分辨「发到哪个群 / 哪台服务器」），路径和查询
    串里的 webhook token、Bark device key 一律不出现——包括查询参数名。URL 过短、
    无法解析或不是 http(s) 时整体返回 ``****``：宁可什么都不显示，也不冒险回显密钥。
    """
    value = (url or "").strip()
    if not value:
        return "****"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "****"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.netloc or len(value) <= _CHANNEL_MASK_MIN_LENGTH:
        return "****"
    return f"{scheme}://{parts.netloc}/****{value[-4:]}"


def mask_channel_target(kind: str, target: str) -> str:
    """按通道类型掩码外发目标（email 逐个掩码，webhook/bark 复用 ``mask_url``）。"""
    if kind != "email":
        return mask_url(target)
    masked = [_mask_email(item) for item in (target or "").replace(";", ",").split(",") if item.strip()]
    return ", ".join(masked) if masked else "****"


def _mask_email(address: str) -> str:
    """``alice@example.com`` → ``a***@example.com``：保留首字母与域名，够区分即可。"""
    local, sep, domain = address.strip().partition("@")
    if not sep or not domain:
        return "****"
    return f"{local[:1]}***@{domain}"


def _channel_name_taken(session: Session, workspace_id: int, name: str, *, exclude_id: int | None = None) -> bool:
    stmt = select(WorkspaceChannel.id).where(
        WorkspaceChannel.workspace_id == workspace_id,
        WorkspaceChannel.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(WorkspaceChannel.id != exclude_id)
    return session.execute(stmt.limit(1)).first() is not None


def _duplicate_channel(name: str) -> WorkspaceError:
    return WorkspaceError(f"该工作区已有同名通道：{name}", status_code=409, code="duplicate_channel")


def create_channel(
    session: Session,
    workspace: Workspace,
    *,
    name: str,
    kind: str,
    url: str,
    enabled: bool = True,
    created_by: int | None = None,
    commit: bool = True,
) -> WorkspaceChannel:
    """新建通道。重名返回 409（预检查 + 唯一约束兜并发）。"""
    cleaned_name = _clean_channel_name(name)
    channel_kind = _clean_channel_kind(kind)
    channel_target = _clean_channel_target(channel_kind, url)
    if _channel_name_taken(session, workspace.id, cleaned_name):
        raise _duplicate_channel(cleaned_name)
    channel = WorkspaceChannel(
        workspace_id=workspace.id,
        name=cleaned_name,
        kind=channel_kind,
        url=channel_target,
        enabled=bool(enabled),
        created_by=created_by,
    )
    session.add(channel)
    try:
        session.flush()
    except IntegrityError as exc:  # 并发下先查后插仍可能撞唯一约束
        session.rollback()
        raise _duplicate_channel(cleaned_name) from exc
    if commit:
        session.commit()
    return channel


def update_channel(
    session: Session,
    workspace: Workspace,
    channel: WorkspaceChannel,
    *,
    name: str | None = None,
    kind: str | None = None,
    url: str | None = None,
    enabled: bool | None = None,
    commit: bool = True,
) -> WorkspaceChannel:
    """改通道。``url=None`` 表示保持原值——API 只返回掩码，前端回传不了明文地址。

    ``kind`` 变更时即使没给新地址，也会用**现有**地址按新 kind 重新校验：email 的
    收件人串与 webhook 的 URL 不通用，静默接受会让通道「建得出来但发不出去」。
    """
    if channel.workspace_id != workspace.id:
        raise not_found("通知通道不存在")
    if name is not None:
        cleaned_name = _clean_channel_name(name)
        if cleaned_name != channel.name and _channel_name_taken(
            session, workspace.id, cleaned_name, exclude_id=channel.id
        ):
            raise _duplicate_channel(cleaned_name)
        channel.name = cleaned_name
    if kind is not None:
        new_kind = _clean_channel_kind(kind)
        channel.url = _clean_channel_target(new_kind, url if url is not None else channel.url)
        channel.kind = new_kind
    elif url is not None:
        channel.url = _clean_channel_target(channel.kind, url)
    if enabled is not None:
        channel.enabled = bool(enabled)
    if commit:
        session.commit()
    return channel


def delete_channel(
    session: Session,
    workspace: Workspace,
    channel: WorkspaceChannel,
    *,
    commit: bool = True,
) -> None:
    if channel.workspace_id != workspace.id:
        raise not_found("通知通道不存在")
    session.delete(channel)
    if commit:
        session.commit()


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
    "CHANNEL_KINDS",
    "CONFIG_MODELS",
    "DATA_MODELS",
    "MAX_CHANNEL_NAME_LENGTH",
    "MAX_CHANNEL_URL_LENGTH",
    "PERSONAL_KIND",
    "SHARED_KIND",
    "WorkspaceError",
    "add_member",
    "all_workspaces",
    "channel_or_404",
    "channels",
    "claim_unassigned",
    "create_channel",
    "create_workspace",
    "data_counts",
    "delete_channel",
    "delete_workspace",
    "ensure_personal_workspace",
    "forbidden",
    "mask_channel_target",
    "mask_url",
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
    "update_channel",
    "update_workspace",
    "workspace_or_404",
]
