"""backtest 插件 —— 策略回测端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class BacktestPlugin(PluginBase):
    """回测插件：信号生成、收益统计与历史记录（单向依赖 strategy / predict）。"""

    name = "backtest"
    version = "0.1.0"
    description = "策略回测：信号生成、收益统计与历史记录"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把回测路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.backtest.routers import backtest

        router.include_router(backtest.router)


plugin = BacktestPlugin()

__all__ = ["BacktestPlugin", "plugin"]
