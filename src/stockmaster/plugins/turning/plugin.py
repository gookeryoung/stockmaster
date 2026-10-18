"""turning 插件 —— A 类债核心顶底识别端点。

方法论移植自量析平台 ``backend/tpcore.py``（口径见 :mod:`stockmaster.core.tpcore`）。
适用对象为低波动类债核心资产（长江电力、银行、高速、港口、核电）；
通用趋势模型（analysis / predict 插件）不适用，两者互补不替代。
"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class TurningPlugin(PluginBase):
    """顶底识别插件：状态扫描、历史事件与模型口径说明。"""

    name = "turning"
    version = "0.1.0"
    description = "A 类债核心顶底识别：七维滚动分位最短板 + 环境闸门 + 右侧确认"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把顶底识别路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.turning.routers import turning

        router.include_router(turning.router)


plugin = TurningPlugin()

__all__ = ["TurningPlugin", "plugin"]
