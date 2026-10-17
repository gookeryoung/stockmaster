"""alerts 插件 —— 规则预警端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class AlertsPlugin(PluginBase):
    """预警插件：规则 CRUD、触发记录、扫描与预览。"""

    name = "alerts"
    version = "0.1.0"
    description = "规则预警：规则 CRUD、触发记录、扫描与预览"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把预警路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.alerts.routers import alerts

        router.include_router(alerts.router)


plugin = AlertsPlugin()

__all__ = ["AlertsPlugin", "plugin"]
