"""market 插件 —— 行情数据端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class MarketPlugin(PluginBase):
    """行情数据插件：标的检索、K 线、快照、指标与市场总览。"""

    name = "market"
    version = "0.1.0"
    description = "行情数据：标的检索、K 线、快照、指标与市场总览"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把行情路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.market.routers import market

        router.include_router(market.router)


plugin = MarketPlugin()

__all__ = ["MarketPlugin", "plugin"]
