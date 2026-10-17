"""workspaces 插件 —— 工作区与成员管理端点。

本插件用 ``route_prefix`` 声明挂载前缀 ``/api/v1/workspaces``：路由装饰器里的路径是
``/{workspace_id}`` 这种相对路径，前缀由注册中心统一施加（与重构前的 main 一致）。

这里额外直接暴露 ``direct_router``（与参考项目 cndb 的 tables 插件同一手法）：
本组路由含路径为空的端点（``GET /api/v1/workspaces``），若由注册中心先包一层空的
APIRouter 再挂载，FastAPI 会因「前缀与路径不能同时为空」直接报错。
"""

from __future__ import annotations

from fastapi import APIRouter

from stockmaster.plugins.base import PluginBase
from stockmaster.plugins.workspaces.routers import workspaces


class WorkspacesPlugin(PluginBase):
    """工作区插件：隔离边界、成员角色与所有权转让。"""

    name = "workspaces"
    version = "0.1.0"
    description = "工作区与成员：隔离边界、成员角色与所有权转让"
    route_prefix = "workspaces"
    # 由 mount_routes 优先使用，避免多一层 include_router 中转（见模块 docstring）。
    direct_router = workspaces.router

    def register_routes(self, router: APIRouter) -> None:
        """``direct_router`` 未生效时的回退路径：把工作区路由挂到传入的 router 上。"""
        router.include_router(workspaces.router)


plugin = WorkspacesPlugin()

__all__ = ["WorkspacesPlugin", "plugin"]
