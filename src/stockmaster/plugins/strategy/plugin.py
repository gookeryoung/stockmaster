"""strategy 插件 —— 工作区级策略配置端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class StrategyPlugin(PluginBase):
    """策略配置插件：选股配置、样本配置与算法配置。"""

    name = "strategy"
    version = "0.1.0"
    description = "工作区级策略配置：选股、样本与算法"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把策略路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.strategy.routers import strategy

        router.include_router(strategy.router)


plugin = StrategyPlugin()

__all__ = ["StrategyPlugin", "plugin"]
