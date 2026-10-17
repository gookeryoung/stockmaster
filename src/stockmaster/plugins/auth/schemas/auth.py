"""Pydantic schemas for accounts, sessions and admin user management."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["admin", "user"]
UserStatus = Literal["active", "disabled"]


class IdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    nickname: str = ""
    avatar_url: str = ""
    created_at: datetime | None = None


class UserOut(BaseModel):
    """Public projection of an account. Never carries the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None = None
    nickname: str = ""
    avatar_url: str = ""
    role: Role = "user"
    status: UserStatus = "active"
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    # Surfaced so the UI can tell a password account from a WeChat-only account.
    has_password: bool = False
    identities: list[IdentityOut] = Field(default_factory=list)


class AdminUserOut(UserOut):
    """管理员用户列表投影：在 ``UserOut`` 之上追加工作区/数据量统计。

    ``personal_data_total`` 只算该账号 **personal 工作区**的数据量（数据属于工作区，
    共享工作区的数据不重复算到每个成员头上）；``owned_workspace_count`` 含 personal。
    """

    workspace_count: int = 0  # 参与的成员关系数
    owned_workspace_count: int = 0  # owner_id 指向该账号的工作区数
    personal_data_total: int = 0  # 其 personal 工作区内的业务数据量


class UserPage(BaseModel):
    total: int
    items: list[AdminUserOut]


class TokenOut(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut


class AuthConfigOut(BaseModel):
    """What the login page needs to know before it renders."""

    auth_enabled: bool
    allow_register: bool
    wechat_enabled: bool
    wechat_mode: str
    has_users: bool
    # 数据按工作区隔离（SM_DATA_ISOLATION）。前端据此决定是否显示工作区切换器
    # 与成员管理：关闭时所有账号共享一份数据，切换工作区没有意义。
    workspace_enabled: bool = False
    version: str = ""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=255, description="用户名或邮箱")
    password: str = Field(min_length=1, max_length=128)


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    nickname: str = Field(default="", max_length=64)


class PasswordChangeIn(BaseModel):
    old_password: str = Field(default="", max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ProfileUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=512)


class WechatUrlOut(BaseModel):
    url: str
    state: str
    mode: str


class TicketExchangeIn(BaseModel):
    ticket: str = Field(min_length=8, max_length=128)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    nickname: str = Field(default="", max_length=64)
    role: Role = "user"
    status: UserStatus = "active"


class UserUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    role: Role | None = None
    status: UserStatus | None = None


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    username: str = ""
    action: str
    detail: str = ""
    ip: str = ""
    created_at: datetime | None = None


__all__ = [
    "AdminUserOut",
    "AuditLogOut",
    "AuthConfigOut",
    "IdentityOut",
    "LoginIn",
    "PasswordChangeIn",
    "PasswordResetIn",
    "ProfileUpdateIn",
    "RegisterIn",
    "TicketExchangeIn",
    "TokenOut",
    "UserCreateIn",
    "UserOut",
    "UserPage",
    "UserUpdateIn",
    "WechatUrlOut",
]
