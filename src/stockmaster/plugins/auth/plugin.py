"""auth 插件 —— 账号、鉴权与用户管理端点。

该插件同时拥有 ``middleware``（登录网关），但中间件不在此注册：仍由 ``main`` 的
``add_middleware`` 显式安装，保证中间件顺序（鉴权在 CORS 之内）不因插件发现而漂移。
"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class AuthPlugin(PluginBase):
    """账号插件：登录注册、会话、微信 OAuth 与管理员用户管理。"""

    name = "auth"
    version = "0.1.0"
    description = "账号与鉴权：登录注册、会话、微信 OAuth 与用户管理"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把账号与管理路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.auth.routers import auth, users

        router.include_router(auth.router)
        router.include_router(users.router)


plugin = AuthPlugin()

__all__ = ["AuthPlugin", "plugin"]
