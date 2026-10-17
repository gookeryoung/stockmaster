"""analysis 插件 —— 指标与因子分析端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class AnalysisPlugin(PluginBase):
    """分析插件：因子快照、横截面筛选与相关性分析。"""

    name = "analysis"
    version = "0.1.0"
    description = "指标与因子分析：因子快照、横截面筛选与相关性"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把分析路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.analysis.routers import analysis

        router.include_router(analysis.router)


plugin = AnalysisPlugin()

__all__ = ["AnalysisPlugin", "plugin"]
