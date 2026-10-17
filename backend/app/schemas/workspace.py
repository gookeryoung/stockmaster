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


__all__ = [
    "MemberIn",
    "MemberRoleIn",
    "TransferIn",
    "WorkspaceDeleted",
    "WorkspaceIn",
    "WorkspaceMemberOut",
    "WorkspaceOut",
    "WorkspaceUpdate",
]
