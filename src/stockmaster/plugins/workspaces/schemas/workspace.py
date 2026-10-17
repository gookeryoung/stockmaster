"""工作区与成员的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str = ""
    kind: str = "shared"  # personal | shared
    owner_id: int | None = None
    # The caller's own role — never the workspace's "primary" owner role, because a
    # workspace can be owned by someone else and the UI needs *my* permissions.
    role: str = "viewer"
    member_count: int = 0
    # 「数据量」口径：自选/规则/回测/三类策略配置，不含成员与通道（见 core.quota）。
    data_total: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkspaceMemberOut(BaseModel):
    user_id: int
    username: str = ""
    nickname: str = ""
    avatar_url: str = ""
    role: str = "viewer"
    joined_at: datetime | None = None


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    # owner 只能通过转让得到，所以这里刻意不接受 owner。
    role: str = Field(default="viewer", pattern="^(editor|viewer)$")


class MemberRoleIn(BaseModel):
    role: str = Field(pattern="^(owner|editor|viewer)$")


class TransferIn(BaseModel):
    user_id: int


class WorkspaceDeleted(BaseModel):
    status: str = "deleted"
    purged: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 工作区级通知通道
# --------------------------------------------------------------------------- #


class ChannelIn(BaseModel):
    """新建通道。

    ``kind`` / ``url`` 的业务校验（合法类型、http(s)、无空白、控制字符）放在服务层，
    这样 CLI 与 API 走同一套规则；非法值由 ``WorkspaceError`` 统一翻译成 422。
    """

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="webhook", min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=512)
    enabled: bool = True


class ChannelUpdate(BaseModel):
    """改通道；字段全部可选。

    ``url=None``（省略或显式传 null）表示**保持原值**，因为 API 只回显掩码，前端
    无法把明文地址原样传回来。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: str | None = Field(default=None, min_length=1, max_length=16)
    url: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool | None = None


class WorkspaceChannelOut(BaseModel):
    """通道的对外形状：``url`` 永远是掩码，明文 token 不会离开服务端。"""

    id: int
    name: str
    kind: str
    url: str
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkspaceUsageOut(BaseModel):
    """工作区用量与配额（只读）。

    ``limits`` 里 ``0`` 表示不限；``labels`` 是资源键的中文名，前端直接展示避免
    第二份翻译表；``total`` 是全部计入资源之和，供「共 N 条工作区数据」使用。
    """

    workspace_id: int
    usage: dict[str, int]
    limits: dict[str, int]
    labels: dict[str, str]
    total: int


class AlertPolicyEffectiveOut(BaseModel):
    """工作区策略与规则合并后的有效值（NULL 已归一）。"""

    exclude_st: bool
    min_amount: float
    cooldown_seconds: int
    once_per_day: bool


class AlertPolicyIn(BaseModel):
    """PUT 工作区预警策略：字段全为 None / 未提供 = 重置（删除策略行）。

    非 None 的维度写入覆盖；单个字段为 None 表示「该维度跟随规则」。策略只能收紧
    不能放宽，合并规则见 ``plugins/alerts/services/policy.py``。
    """

    exclude_st: bool | None = None
    min_amount: float | None = Field(default=None, ge=0, le=1_000_000_000)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=30 * 24 * 3600)
    once_per_day: bool | None = None


class AlertPolicyOut(BaseModel):
    """策略行原值（未设置 = null）与当前有效值。"""

    workspace_id: int
    exclude_st: bool | None = None
    min_amount: float | None = None
    cooldown_seconds: int | None = None
    once_per_day: bool | None = None
    updated_at: datetime | None = None
    effective: AlertPolicyEffectiveOut


__all__ = [
    "AlertPolicyEffectiveOut",
    "AlertPolicyIn",
    "AlertPolicyOut",
    "ChannelIn",
    "ChannelUpdate",
    "MemberIn",
    "MemberRoleIn",
    "TransferIn",
    "WorkspaceChannelOut",
    "WorkspaceDeleted",
    "WorkspaceIn",
    "WorkspaceMemberOut",
    "WorkspaceOut",
    "WorkspaceUpdate",
    "WorkspaceUsageOut",
]
