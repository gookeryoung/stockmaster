"""system 插件 —— 健康检查、自选股、系统状态与备份恢复端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class SystemPlugin(PluginBase):
    """系统插件：健康检查、自选股、系统状态、数据质量与管理员备份恢复。"""

    name = "system"
    version = "0.1.0"
    description = "系统：健康检查、自选股、系统状态、数据质量与备份恢复"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把系统与管理员路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.system.routers import admin, system

        router.include_router(system.router)
        router.include_router(admin.router)


plugin = SystemPlugin()

__all__ = ["SystemPlugin", "plugin"]
