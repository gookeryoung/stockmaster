"""workspaces 插件 —— 工作区、成员、通道、用量与趋势端点。

本插件用 ``route_prefix`` 声明挂载前缀 ``/api/v1/workspaces``：路由装饰器里的路径是
``/{workspace_id}`` 这种相对路径，前缀由注册中心统一施加（与重构前的 main 一致）。

这里额外直接暴露 ``direct_router``（与参考项目 cndb 的 tables 插件同一手法）：
本组路由含路径为空的端点（``GET /api/v1/workspaces``），若由注册中心先包一层空的
APIRouter 再挂载，FastAPI 会因「前缀与路径不能同时为空」直接报错。用量趋势/导出
是同一前缀下的只读视图，因此在 ``direct_router`` 里与主路由组合，而不是再注册
一个插件（那会重复施加 ``/workspaces`` 前缀）。
"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase
from stockmaster.plugins.workspaces.routers import usage, workspaces


def _build_router() -> APIRouter:
    """把用量趋势路由合进工作区主路由，返回可直接挂载的 router。

    注意方向：**不能**先建一个空 ``APIRouter`` 再 include 主路由 —— 主路由里有路径
    为 ``""`` 的 ``GET /api/v1/workspaces``，FastAPI 不允许「前缀与路径同时为空」的
    中间层（否则插件加载直接失败）。反过来把非空的用量路由 include 进主路由即可，
    空路径端点的行为与引入本功能前完全一致。
    """
    workspaces.router.include_router(usage.router)
    return workspaces.router


class WorkspacesPlugin(PluginBase):
    """工作区插件：隔离边界、成员角色、通知通道与用量趋势。"""

    name = "workspaces"
    version = "0.1.0"
    description = "工作区与成员：隔离边界、成员角色、通知通道与用量趋势"
    route_prefix = "workspaces"
    # 由 mount_routes 优先使用，避免多一层 include_router 中转（见模块 docstring）。
    direct_router = _build_router()

    def register_routes(self, router: APIRouter) -> None:
        """``direct_router`` 未生效时的回退路径：把工作区（含用量）路由挂到传入的 router 上。"""
        router.include_router(workspaces.router)


plugin = WorkspacesPlugin()

__all__ = ["WorkspacesPlugin", "plugin"]
