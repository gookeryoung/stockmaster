"""predict 插件 —— 机器学习预测端点。"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase


class PredictPlugin(PluginBase):
    """预测插件：数据集构建、模型训练、评估与打分。"""

    name = "predict"
    version = "0.1.0"
    description = "机器学习预测：数据集构建、模型训练、评估与打分"
    route_prefix = ""

    def register_routes(self, router: APIRouter) -> None:
        """把预测路由挂到注册中心传入的 router 上。"""
        from stockmaster.plugins.predict.routers import predict

        router.include_router(predict.router)


plugin = PredictPlugin()

__all__ = ["PredictPlugin", "plugin"]
